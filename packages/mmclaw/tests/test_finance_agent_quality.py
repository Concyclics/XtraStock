from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from xtrastock.config import Settings
ANALYTICS_DIR = Settings().path('paths.app') / "scripts" / "analytics"
if str(ANALYTICS_DIR) not in sys.path:
    sys.path.insert(0, str(ANALYTICS_DIR))

from mmclaw.config import ConfigManager
from mmclaw.kernel import MMClaw
from mmclaw.memory import FileMemory
from mmclaw.providers.openai_compatible import OpenAICompatibleProvider
from mmclaw.tool_control import ToolLoopController
from mmclaw.tool_schemas import get_native_tool_schemas
from mmclaw.tools import FinanceDBTool, SafeCalculatorTool
from classify_ai_hardware_candidates import classify_by_rules
from extract_document_events_rules import CompanyResolver, extract as extract_document_events


class ToolLoopControllerTests(unittest.TestCase):
    def test_reuses_exact_duplicate_without_ending_other_research(self):
        control = ToolLoopController(max_rounds=6, max_calls=12, duplicate_limit=1)
        self.assertTrue(control.begin_round())
        first = control.before_call("finance_asset_snapshot", {"stock_code": "000001"})
        self.assertTrue(first.execute)
        control.after_call("finance_asset_snapshot", '{"meta":{"row_count":1},"rows":[]}')

        duplicate = control.before_call("finance_asset_snapshot", {"stock_code": "000001"})
        self.assertFalse(duplicate.execute)
        self.assertEqual(duplicate.reason, "cached_result")
        self.assertIn('"row_count":1', duplicate.result)
        self.assertFalse(control.force_final)
        self.assertEqual(control.executed_calls, 1)
        self.assertTrue(control.before_call("finance_market_series_batch", {"stock_codes":["000001"]}).execute)

    def test_call_budget_is_hard_limit(self):
        control = ToolLoopController(max_rounds=6, max_calls=2)
        control.begin_round()
        self.assertTrue(control.before_call("a", {"n": 1}).execute)
        self.assertTrue(control.before_call("a", {"n": 2}).execute)
        blocked = control.before_call("a", {"n": 3})
        self.assertFalse(blocked.execute)
        self.assertEqual(control.executed_calls, 2)
        self.assertEqual(control.stop_reason, "max_calls")

    def test_repeated_error_opens_circuit(self):
        control = ToolLoopController(same_error_limit=2)
        control.begin_round()
        for index in (1, 2):
            decision = control.before_call("finance_data_context", {"attempt": index})
            self.assertTrue(decision.execute)
            control.after_call("finance_data_context", "FinanceDB error: permission denied for schema agent")
        self.assertFalse(control.force_final)
        self.assertIn("finance_data_context", control.disabled_tools)
        self.assertFalse(control.before_call("finance_data_context", {"attempt":3}).execute)
        self.assertTrue(control.before_call("finance_market_series_batch", {"stock_codes":["000001"]}).execute)


class FinanceChatPromptTests(unittest.TestCase):
    def setUp(self):
        ConfigManager.mode = "qq"
        self.config = {
            "engine_type": "deepseek",
            "tool_calling_mode": "native",
            "tool_profile": "finance_chat_readonly",
            "allowed_tools": [],
            "disabled_tools": [],
            "engines": {"deepseek": {"model": "deepseek-chat"}},
        }

    def test_prompt_matches_active_tool_schema(self):
        prompt = ConfigManager.get_full_prompt(self.config)
        schemas = get_native_tool_schemas(self.config)
        enabled = {schema["function"]["name"] for schema in schemas}
        self.assertIn("finance_market_screen", enabled)
        self.assertIn("finance_time_context", enabled)
        self.assertIn("finance_data_status", enabled)
        self.assertIn("finance_resolve_entities", enabled)
        self.assertIn("finance_document_retrieve", enabled)
        self.assertIn("finance_research_bundle", enabled)
        self.assertNotIn("finance_db_query", enabled)
        self.assertLessEqual(len(enabled), 10)
        self.assertIn("exact evidence_id", prompt)
        for disabled in (
            "finance_db_query", "finance_db_schema", "finance_plot_chart",
            "finance_document_search", "finance_document_bm25_search",
            "finance_document_evidence", "finance_screen_stocks", "finance_stock_profile",
            "finance_research_momentum", "finance_event_study", "shell_execute", "file_read",
        ):
            self.assertNotIn(disabled, prompt)
        self.assertLess(len(prompt), 12_000)
        self.assertIn("actual, forecast, consensus", prompt)
        self.assertIn("Paths are associations, not causality", prompt)
        self.assertIn("available_at and decision_cutoff", prompt)
        self.assertIn("hard data gate fails", prompt)

    def test_json_mode_prompt_describes_json_tool_contract(self):
        self.config["tool_calling_mode"] = "json"
        prompt = ConfigManager.get_full_prompt(self.config)
        self.assertIn("exactly one valid JSON object", prompt)
        self.assertIn('"tools":[]', prompt)
        self.assertNotIn("Use the provided native tools", prompt)


class TemporalEntityResolverTests(unittest.TestCase):
    def test_rule_extractor_resolves_exact_company_name(self):
        resolver = CompanyResolver([{"symbol": "600030", "name": "中信证券"}])
        events, _ = extract_document_events({
            "document_id": "doc-1", "title": "中信证券业绩预增",
            "content_text": "中信证券发布业绩预增公告。", "event_date": "2026-07-16",
            "source_type": "news", "source_name": "test",
        }, "rules:v2", resolver)
        self.assertTrue(events)
        self.assertIn("600030", events[0]["stock_codes"])

    def test_rule_extractor_does_not_link_distant_industry_to_event(self):
        events, _ = extract_document_events({
            "document_id": "doc-2", "title": "综合新闻",
            "content_text": "半导体行业动态。" + ("其他内容" * 100) + "企业接受尽职调查。",
            "event_date": "2026-07-16", "source_type": "news", "source_name": "test",
        }, "rules:v2")
        risk = next(event for event in events if event["event_type"] == "regulation_risk")
        self.assertNotIn("半导体", risk["industries"])


class ResearchWorkflowTests(unittest.TestCase):
    @staticmethod
    def _payload(tool, semantic=False):
        meta = {"row_count": 1, "source_date": "2026-07-16"}
        if semantic:
            meta.update({"semantic_used": True, "semantic_coverage_pct": 42.5})
        return json.dumps({
            "meta": meta,
            "rows": [{
                "stock_code": "000001", "stock_name": "平安银行",
                "as_of_date": "2026-07-16", "title": tool,
                "pe_ttm": 6.5, "source_type": "database_fact",
            }],
        }, ensure_ascii=False)

    def test_company_workflow_builds_verified_evidence_ledger(self):
        methods = {
            "asset_snapshot": self._payload("snapshot"),
            "fundamental_analysis": self._payload("fundamentals"),
            "relative_valuation": self._payload("valuation"),
            "capital_flow_analysis": self._payload("flow"),
            "forecast_trends": self._payload("forecast"),
            "factor_profile": self._payload("factor"),
            "graph_paths": self._payload("graph"),
            "document_retrieve": self._payload("document", semantic=True),
            "macro_context": self._payload("macro"),
        }
        patches = [mock.patch.object(FinanceDBTool, name, return_value=value) for name, value in methods.items()]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        payload = json.loads(FinanceDBTool.research_workflow(
            workflow_type="company_due_diligence", stock_code="000001", days=180, limit=4
        ))

        self.assertEqual(payload["verification"]["status"], "complete")
        self.assertEqual(payload["verification"]["semantic_coverage_pct"], 42.5)
        self.assertTrue(payload["meta"]["deterministic_workflow"])
        self.assertTrue(payload["evidence_ledger"])
        self.assertTrue(all(row["evidence_id"].startswith("E") for row in payload["evidence_ledger"]))
        self.assertIn("evidence", payload["answer_contract"]["required_sections"])

    def test_workflow_rejects_missing_subject(self):
        self.assertTrue(FinanceDBTool.research_workflow("theme_research").startswith("Error:"))
        self.assertTrue(FinanceDBTool.research_workflow("company_due_diligence").startswith("Error:"))

    def test_signal_explain_requires_factor_graph_and_documents(self):
        methods = {
            "asset_snapshot": self._payload("snapshot"),
            "factor_attribution": self._payload("factor_attribution"),
            "factor_readiness": self._payload("factor_readiness"),
            "factor_catalog": self._payload("factor_catalog"),
            "graph_paths": self._payload("graph_paths"),
            "forecast_trends": self._payload("forecast"),
            "document_retrieve": self._payload("document", semantic=True),
        }
        patches = [mock.patch.object(FinanceDBTool, name, return_value=value) for name, value in methods.items()]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        payload = json.loads(FinanceDBTool.research_workflow(
            workflow_type="signal_explain", stock_code="300308", days=30, limit=3
        ))
        self.assertEqual(payload["verification"]["status"], "complete")
        component_names = {item["tool"] for item in payload["components"]}
        self.assertIn("finance_factor_attribution", component_names)
        self.assertIn("finance_factor_readiness", component_names)
        self.assertIn("finance_graph_paths", component_names)


class SafeCalculatorTests(unittest.TestCase):
    def calculate(self, expression, variables=None, precision=6, unit=None):
        return json.loads(SafeCalculatorTool.calculate(expression, variables, precision, unit))

    def test_decimal_percentage_is_exact_and_auditable(self):
        result = self.calculate(
            "(target_price / current_price - 1) * 100",
            {"target_price": "100", "current_price": "80"},
            2,
            "%",
        )
        self.assertEqual(result["result"], "25")
        self.assertEqual(result["rounded_result"], "25.00")
        self.assertEqual(result["unit"], "%")
        self.assertEqual(result["numeric_engine"], "decimal_50_digits")

    def test_financial_and_statistical_functions(self):
        cagr = self.calculate("cagr(begin, end, years) * 100", {"begin": 100, "end": 121, "years": 2}, 4)
        average = self.calculate("weighted_average(values, weights)", {"values": [10, 20], "weights": [1, 3]}, 2)
        self.assertEqual(cagr["rounded_result"], "10.0000")
        self.assertEqual(average["rounded_result"], "17.50")

    def test_rejects_arbitrary_python_and_dangerous_magnitude(self):
        rejected = (
            "__import__('os').system('id')",
            "(1).__class__",
            "[x for x in [1, 2]]",
            "2 ** 101",
        )
        for expression in rejected:
            with self.subTest(expression=expression):
                result = SafeCalculatorTool.calculate(expression)
                self.assertTrue(result.startswith("Error: finance_calculator rejected expression:"), result)

    def test_rejects_division_by_zero(self):
        result = SafeCalculatorTool.calculate("revenue / shares", {"revenue": 10, "shares": 0})
        self.assertTrue(result.startswith("Error: finance_calculator rejected expression:"), result)

    def test_rejects_invalid_variables_and_function_arity(self):
        invalid_variables = SafeCalculatorTool.calculate("1 + 1", [])
        invalid_arity = SafeCalculatorTool.calculate("sqrt(1, 2)")
        self.assertTrue(invalid_variables.startswith("Error: finance_calculator rejected expression:"))
        self.assertTrue(invalid_arity.startswith("Error: finance_calculator rejected expression:"))


class ProviderFailureTests(unittest.TestCase):
    def setUp(self):
        self.provider = OpenAICompatibleProvider({
            "engine_type": "deepseek",
            "stream": False,
            "engines": {
                "deepseek": {
                    "model": "deepseek-chat",
                    "api_key": "test-key",
                    "base_url": "https://example.invalid",
                }
            },
        })

    def test_network_error_is_retried_then_raised(self):
        calls = []

        def fail(*args, **kwargs):
            calls.append(1)
            raise urllib.error.URLError("temporary dns failure")

        self.provider._ask_blocking = fail
        with mock.patch("mmclaw.providers.openai_compatible.time.sleep"):
            with self.assertRaises(urllib.error.URLError):
                self.provider.ask([{"role": "user", "content": "test"}], retry=1)
        self.assertEqual(len(calls), 2)

    def test_user_error_message_does_not_leak_exception(self):
        message = MMClaw._provider_error_message(
            urllib.error.URLError("secret internal endpoint"), "1234567890"
        )
        self.assertIn("网络连接失败", message)
        self.assertNotIn("12345678", message)
        self.assertNotIn("secret internal endpoint", message)


class KnowledgeGraphGuardTests(unittest.TestCase):
    def test_financial_service_context_cannot_be_direct_exposure(self):
        result = classify_by_rules({
            "candidate_type": "company_exposure",
            "company_relation_type": "direct_product",
            "evidence": "中信证券独家保荐国产GPU企业IPO并提供证券承销服务",
            "normalized_name": "GPU",
            "name": "GPU",
            "parent_name": "算力芯片",
            "concept_type": "component",
            "relation_type": "contains",
            "rule_score": 0.99,
        })
        self.assertFalse(result["is_ai_hardware_related"])
        self.assertEqual(result["company_relation_type"], "weak_mention")

    def test_event_extractor_does_not_treat_table_numbers_as_stock_codes(self):
        row = {
            "document_id": "doc-1", "title": "行业周报",
            "content_text": "指数000001，样本000300，价格上涨",
            "stock_code": None, "report_type": "industry", "industry_name": "建材",
            "event_date": "2026-07-16",
        }
        events, _ = extract_document_events(row, "rules:v2")
        self.assertTrue(events)
        self.assertEqual(events[0]["stock_codes"], [])

    def test_event_extractor_keeps_explicit_company_report_code(self):
        row = {
            "document_id": "doc-2", "title": "公司点评", "content_text": "订单增长",
            "stock_code": "000001", "stock_name": "平安银行", "report_type": "company",
            "industry_name": "银行", "event_date": "2026-07-16",
        }
        events, _ = extract_document_events(row, "rules:v2")
        self.assertEqual(events[0]["stock_codes"], ["000001"])


class EvidenceSummaryTests(unittest.TestCase):
    def test_trace_is_independent_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = FileMemory("system", sessions_dir=tmp)
            memory.append_trace({"event": "tool_call", "tool": "finance_market_screen"})
            trace_path = Path(memory.trace_file)
            self.assertTrue(trace_path.exists())
            row = json.loads(trace_path.read_text(encoding="utf-8").strip())
            self.assertEqual(row["event"], "tool_call")
            self.assertEqual(row["tool"], "finance_market_screen")
            self.assertNotEqual(trace_path, Path(memory.session_file))

    def test_summary_retains_evidence_without_shell_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = FileMemory("system", sessions_dir=tmp)
            dropped = [
                {"role": "user", "content": "查一下最近的研报"},
                {
                    "role": "tool",
                    "name": "finance_document_retrieve",
                    "content": json.dumps({
                        "meta": {"row_count": 1, "search_backend": "postgres_fts"},
                        "rows": [{
                            "document_id": "doc-1", "title": "测试研报",
                            "source_name": "eastmoney", "published_date": "2026-07-16",
                            "stock_code": "000001",
                        }],
                    }, ensure_ascii=False),
                },
            ]
            summary = memory._refresh_summary(dropped)
            note = memory._get_history_note(2)
            self.assertIn("doc-1", summary)
            self.assertIn("postgres_fts", summary)
            self.assertIn("2026-07-16", summary)
            self.assertNotIn("shell_execute", note)
            self.assertNotIn("grep", note)


if __name__ == "__main__":
    unittest.main()
