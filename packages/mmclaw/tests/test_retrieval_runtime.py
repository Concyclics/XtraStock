import json
import subprocess
import unittest
from unittest import mock

from mmclaw.evidence import EvidenceStore
from mmclaw.finance_db_helper import semantic_document_rows
from mmclaw.kernel import MMClaw
from mmclaw.tool_control import ToolLoopController
from mmclaw.tools import FinanceDBTool


class RetrievalDeadlineTests(unittest.TestCase):
    def test_timeout_is_a_tool_error_without_partial_output_or_secret_arguments(self):
        failure = subprocess.TimeoutExpired(
            ["helper", "private-argument"], 22, output=b"partial private output"
        )
        with mock.patch("mmclaw.tools.subprocess.run", side_effect=failure):
            output = FinanceDBTool._run_child(["v3_research_bundle"], timeout=22)
        self.assertIn("data tool timed out", output)
        self.assertNotIn("private", output)
        self.assertIsNotNone(ToolLoopController.result_error_key("finance_research_bundle", output))
        store = EvidenceStore("a" * 32, "分析公司")
        self.assertEqual(0, store.add_tool_result("finance_research_bundle", output))
        self.assertTrue(store.tool_errors)

    def test_unexpected_child_timeout_is_not_reported_as_provider_failure(self):
        output = MMClaw._provider_error_message(
            subprocess.TimeoutExpired(["private-command"], 22), "123456789"
        )
        self.assertIn("数据查询工具超时", output)
        self.assertNotIn("模型服务", output)
        self.assertNotIn("private", output)

    def test_cold_embedding_http_deadline_fits_both_parent_tools(self):
        env = {
            "FINANCE_EMBEDDING_PROVIDER": "openai_compatible",
            "FINANCE_EMBEDDING_API_KEY_ENV": "TEST_EMBED_KEY",
            "TEST_EMBED_KEY": "test-only",
        }
        response = mock.Mock()
        response.json.return_value = {"data": []}
        with mock.patch.dict("os.environ", env), mock.patch(
            "mmclaw.finance_db_helper.requests.post", return_value=response
        ) as post:
            semantic_document_rows("", "公司", [], 30, "", 5, 200)
        connect, read = post.call_args.kwargs["timeout"]
        self.assertGreaterEqual(read, 60)
        with mock.patch.object(FinanceDBTool, "_v3_child", return_value="ok") as child:
            FinanceDBTool.document_retrieve_v3(query="公司")
            self.assertGreater(child.call_args.kwargs["timeout"], connect + read + 25)
            FinanceDBTool.research_bundle(stock_name="公司")
            self.assertGreater(child.call_args.kwargs["timeout"], connect + read + 60)


class EvidenceOutputBudgetTests(unittest.TestCase):
    def payload(self, status="ok"):
        return {
            "meta": {"status": status, "row_count": 10,
                     "recommendation_allowed": False, "warnings": ["original data gap"]},
            "rows": [{"snippet": "duplicate text " * 100} for _ in range(10)],
            "evidence": [{
                "evidence_id": "a" * 32 + f":E{index:04d}",
                "claim_type": "document_claim", "value": {"snippet": "公司业务" * 100},
                "quality_status": "unverified",
            } for index in range(1, 11)],
        }

    def test_oversized_result_keeps_valid_json_and_whole_citable_evidence(self):
        original = self.payload()
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(original, ensure_ascii=False), stderr="")
        with mock.patch.object(FinanceDBTool, "MAX_OUTPUT_CHARS", 2600), mock.patch(
            "mmclaw.tools.subprocess.run", return_value=result
        ):
            output = FinanceDBTool._run_child(["v3_research_bundle"])
        payload = json.loads(output)
        self.assertLessEqual(len(output), 2600)
        self.assertGreater(len(payload["evidence"]), 0)
        self.assertLess(len(payload["evidence"]), 10)
        self.assertEqual(original["evidence"][:len(payload["evidence"])], payload["evidence"])
        self.assertEqual(10, payload["meta"]["evidence_count_available"])
        self.assertEqual(10, payload["meta"]["rows_omitted_from_output"])
        self.assertFalse(payload["meta"]["recommendation_allowed"])
        self.assertIn("original data gap", payload["meta"]["warnings"])
        store = EvidenceStore("b" * 32, "分析公司")
        self.assertEqual(len(payload["evidence"]), store.add_tool_result("finance_research_bundle", output))

    def test_compaction_does_not_bypass_a_blocked_quality_gate(self):
        with mock.patch.object(FinanceDBTool, "MAX_OUTPUT_CHARS", 2600):
            output = FinanceDBTool._bound_child_output(json.dumps(self.payload("blocked"), ensure_ascii=False))
        self.assertEqual("blocked", json.loads(output)["meta"]["status"])
        store = EvidenceStore("b" * 32, "分析公司")
        self.assertEqual(0, store.add_tool_result("finance_research_bundle", output))

    def test_long_financial_history_does_not_erase_other_bundle_components(self):
        payload = self.payload()
        payload['evidence'] = []
        for component, count in [('market', 1), ('actual_fundamental', 100),
                                 ('factor', 20), ('graph', 15), ('document', 8)]:
            for i in range(count):
                payload['evidence'].append({
                    'evidence_id': f'{component}:E{i:04d}', 'claim_type': component,
                    'value': {'component': component, 'detail': '原始资料' * 100},
                })
        original = {row['evidence_id']: row for row in payload['evidence']}
        with mock.patch.object(FinanceDBTool, 'MAX_OUTPUT_CHARS', 6000):
            result = FinanceDBTool._bound_child_output(json.dumps(payload, ensure_ascii=False))
        output = json.loads(result)
        self.assertLessEqual(len(result), 6000)
        self.assertEqual({'market', 'actual_fundamental', 'factor', 'graph', 'document'},
                         {row['value']['component'] for row in output['evidence']})
        for row in output['evidence']:
            self.assertEqual(original[row['evidence_id']], row)
        self.assertEqual(100, output['meta']['evidence_components_available']['actual_fundamental'])

    def test_shared_research_duplicate_metadata_does_not_displace_citations(self):
        payload=self.payload()
        payload['meta']['shared_research']={'status':'available','snapshot_id':'saved','rows':[{'large':'x'*20000}],'watchlist':[]}
        payload['evidence'].append({'evidence_id':'shared:E0001','claim_type':'model_signal',
            'value':{'component':'shared_research','watchlist_membership':{'selected':False},
                'scenarios':[{'prediction_eligible':False,'blockers':['not validated']}]}})
        with mock.patch.object(FinanceDBTool,'MAX_OUTPUT_CHARS',3500):
            output=json.loads(FinanceDBTool._bound_child_output(json.dumps(payload,ensure_ascii=False)))
        self.assertIn('shared:E0001',[r['evidence_id'] for r in output['evidence']])
        self.assertNotIn('rows',output['meta']['shared_research'])
        self.assertEqual(output['meta']['shared_research']['snapshot_id'],'saved')

    def test_oversized_non_envelope_json_is_explicitly_rejected(self):
        with mock.patch.object(FinanceDBTool, "MAX_OUTPUT_CHARS", 100):
            output = FinanceDBTool._bound_child_output(json.dumps({"data": "a" * 1000}))
        self.assertTrue(output.startswith("FinanceDB error:"))
        self.assertNotIn("[truncated]", output)

    def test_repair_ledger_remains_valid_json_with_exact_evidence_ids(self):
        store = EvidenceStore("b" * 32, "分析公司")
        payload = self.payload()
        for item in payload["evidence"]:
            item["value"]["snippet"] *= 10
        store.add_tool_result("finance_research_bundle", json.dumps(payload))
        prompt = store.repair_prompt("unsupported draft", {"violations": []})
        ledger = json.loads(prompt.split("Evidence ledger:\n", 1)[1])
        self.assertGreater(len(ledger), 0)
        self.assertLess(len(ledger), len(payload["evidence"]))
        self.assertEqual(payload["evidence"][0]["evidence_id"], ledger[0]["evidence_id"])

    def test_repair_excludes_failed_tool_history_and_preserves_system_and_question(self):
        kernel = MMClaw.__new__(MMClaw)
        kernel.config = {"tool_profile": "finance_chat_readonly"}
        kernel.debug = False
        kernel._ask_with_stop = mock.Mock(return_value={"content": "当前证据不足，无法确认。"})
        kernel._trace = mock.Mock()
        store = EvidenceStore("b" * 32, "分析公司")
        store.persist = mock.Mock()
        store.persist_audit = mock.Mock()
        history = [
            {"role": "system", "content": "system rules"},
            {"role": "user", "content": "rejected tool output with invalid IDs"},
        ]
        with mock.patch("mmclaw.kernel.persist_model_call"):
            answer = kernel._guard_finance_answer("股票价格999元", store, history, False)
        messages = kernel._ask_with_stop.call_args.args[0]
        self.assertEqual(history[0], messages[0])
        self.assertIn("分析公司", messages[1]["content"])
        self.assertNotIn("rejected tool output", json.dumps(messages))
        self.assertEqual("当前证据不足，无法确认。", answer)


if __name__ == "__main__":
    unittest.main()
