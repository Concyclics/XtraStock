import contextlib
import io
import json
import unittest
from unittest import mock

from mmclaw.evidence import EvidenceStore
from mmclaw.finance_db_helper import run_v3_time_context
from mmclaw.kernel import MMClaw


REQUEST_ID = "01234567-89ab-cdef-0123-456789abcdef"


class TimeEvidenceTests(unittest.TestCase):
    def test_real_clock_is_citable_and_numeric_clock_fields_are_supported(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            run_v3_time_context("", {"request_id": REQUEST_ID, "timezone": "Asia/Shanghai"})
        payload = json.loads(output.getvalue())
        evidence = payload["evidence"][0]
        value = evidence["value"]
        self.assertEqual("system:clock", evidence["source_id"])
        self.assertEqual(payload["rows"][0]["current_time"], value["current_time"])
        store = EvidenceStore(REQUEST_ID, "现在是北京时间几点")
        self.assertEqual(1, store.add_tool_result("finance_time_context", output.getvalue()))
        answer = (
            f"北京时间 {value['today']} {value['hour']:02d}:"
            f"{value['minute']:02d}:{value['second']:02d} [{evidence['evidence_id']}]。"
        )
        self.assertTrue(store.validate(answer)["passed"])
        self.assertFalse(store.validate(answer.replace(evidence["evidence_id"], "f" * 32 + ":E0001"))["passed"])

    def test_time_evidence_does_not_support_a_stock_price(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            run_v3_time_context("", {"request_id": REQUEST_ID})
        payload = json.loads(output.getvalue())
        store = EvidenceStore(REQUEST_ID, "股票价格是多少")
        store.add_tool_result("finance_time_context", output.getvalue())
        answer = f"股票价格为987654.321元 [{payload['evidence'][0]['evidence_id']}]。"
        self.assertFalse(store.validate(answer)["passed"])


class RepairEnvelopeTests(unittest.TestCase):
    def guard(self, response):
        kernel = MMClaw.__new__(MMClaw)
        kernel.config = {"tool_profile": "finance_chat_readonly"}
        kernel.debug = False
        kernel._ask_with_stop = mock.Mock(return_value={"content": response})
        kernel._trace = mock.Mock()
        store = EvidenceStore(REQUEST_ID, "股票价格是多少")
        store.persist = mock.Mock()
        store.persist_audit = mock.Mock()
        with mock.patch("mmclaw.kernel.persist_model_call"):
            result = kernel._guard_finance_answer("股票价格999元", store, [], False)
        return result, store

    def test_json_repair_envelope_is_not_sent_to_user(self):
        final = "当前没有足够证据，无法确认股票价格。"
        result, store = self.guard(json.dumps({"thought": "private", "tools": [], "content": final}, ensure_ascii=False))
        self.assertEqual(final, result)
        self.assertEqual(final, store.persist_audit.call_args.args[1])

    def test_plain_text_repair_is_accepted(self):
        final = "当前没有足够证据，无法确认股票价格。"
        result, _ = self.guard(final)
        self.assertEqual(final, result)

    def test_tool_calls_in_repair_are_rejected(self):
        result, _ = self.guard(json.dumps({"tools": [{"name": "shell_execute"}], "content": "ok"}))
        self.assertIn("没有返回可引用", result)
        self.assertNotIn("shell_execute", result)

    def test_numeric_claim_inside_repair_json_is_still_checked(self):
        result, _ = self.guard(json.dumps({"tools": [], "content": "股票价格888元"}, ensure_ascii=False))
        self.assertIn("没有返回可引用", result)
        self.assertNotIn("888", result)


if __name__ == "__main__":
    unittest.main()
