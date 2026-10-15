"""Regression: repeat company analysis in a persisted QQ group after a failed turn."""

import contextlib
import copy
import io
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mmclaw.finance_db_helper import run_entity_resolve
from mmclaw.evidence import EvidenceStore
from mmclaw.kernel import MMClaw
from mmclaw.memory import FileMemory, StatelessMemory
from mmclaw.tool_control import ToolLoopController


OLD_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa:E0001"
NEW_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb:E0001"
OLD_CUTOFF = "2026-10-12T15:22:38.897136+08:00"


class FinanceTurnContextTests(unittest.TestCase):
    def make_memory(self):
        memory = StatelessMemory("Finance prompt")
        memory.add("user", "分析一下长鑫")
        memory.add("assistant", json.dumps({"tools": [{"name": "finance_research_bundle", "args": {
            "stock_code": "688825", "decision_cutoff": OLD_CUTOFF,
        }}], "content": ""}))
        memory.add("user", "Tool Output (finance_research_bundle):\n" + json.dumps({
            "meta": {"status": "blocked", "decision_cutoff": OLD_CUTOFF},
            "evidence": [{"evidence_id": OLD_ID}],
        }))
        memory.add("assistant", json.dumps({"tools": [], "content": f"长鑫科技 [{OLD_ID}]"}))
        return memory

    def test_old_tool_cutoff_gate_and_citations_cannot_leak_into_new_turn(self):
        memory = self.make_memory()
        start = len(memory.history)
        memory.add("user", "分析一下长鑫")
        current_result = "Tool Output (finance_research_bundle):\n" + json.dumps({
            "meta": {"status": "ok"}, "evidence": [{"evidence_id": NEW_ID}],
        })
        memory.add("user", current_result)
        before = copy.deepcopy(memory.history)
        messages = memory.get_for_finance_turn(start)
        serialized = json.dumps(messages, ensure_ascii=False)
        self.assertNotIn(OLD_CUTOFF, serialized)
        self.assertNotIn(OLD_ID, serialized)
        self.assertNotIn('"blocked"', serialized)
        self.assertIn("长鑫科技", serialized)  # follow-up entity context survives
        self.assertEqual(messages[-1]["content"], current_result)
        self.assertEqual(memory.history, before)  # original transcript untouched

    def test_native_tool_protocol_is_preserved_only_in_current_turn(self):
        memory = self.make_memory()
        old = {"role": "assistant", "tool_calls": [{"id": "old", "name": "finance_data_status"}]}
        memory.add_message(old)
        memory.add_message({"role": "tool", "tool_call_id": "old", "content": "old gate"})
        start = len(memory.history)
        memory.add("user", "分析长鑫")
        current = {"role": "assistant", "tool_calls": [{"id": "new", "name": "finance_time_context"}]}
        memory.add_message(current)
        result = {"role": "tool", "tool_call_id": "new", "content": "new clock"}
        memory.add_message(result)
        messages = memory.get_for_finance_turn(start)
        self.assertEqual(messages[-2:], [current, result])
        self.assertNotIn("old gate", json.dumps(messages))

    def test_explicit_historical_request_and_followup_context_survive(self):
        memory = self.make_memory()
        memory.add("user", "请按2026-08-28收盘时可知的信息分析长鑫")
        start = len(memory.history)
        memory.add("user", "继续分析财务，保持这个历史截点")
        text = json.dumps(memory.get_for_finance_turn(start), ensure_ascii=False)
        self.assertIn("2026-08-28", text)
        self.assertIn("保持这个历史截点", text)
        self.assertNotIn(OLD_CUTOFF, text)

    def test_internal_followup_instructions_are_not_user_history(self):
        memory = self.make_memory()
        memory.add_message({"role": "user", "content": "old forced instruction", "mmclaw_internal": True})
        start = len(memory.history)
        memory.add("user", "下一家公司")
        self.assertNotIn("old forced instruction", json.dumps(memory.get_for_finance_turn(start)))


class PersistedAnswerTests(unittest.TestCase):
    def test_both_protocols_save_the_delivered_answer_instead_of_rejected_draft(self):
        for protocol in ("native", "json"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as folder:
                agent = MMClaw.__new__(MMClaw)
                agent.config = {"tool_profile": "finance_chat_readonly", "tool_calling_mode": protocol,
                                "agentic_loop": {"enabled": False}}
                agent.memory = FileMemory("prompt", sessions_dir=folder)
                agent.memory.GLOBAL_MEMORY_FILE = None
                agent.connector = mock.Mock()
                agent.engine = mock.Mock(supports_native_tools=protocol == "native")
                agent.use_stateless_arg_connector = False
                agent.debug = False
                agent._stop_event = threading.Event()
                agent._activate_task_context = mock.Mock()
                agent._trace = mock.Mock()
                agent._guard_finance_answer = mock.Mock(return_value="这是经过证据校验后实际发出的答复")
                draft = "REJECTED_DRAFT: 收入999999亿元"
                response = draft if protocol == "native" else json.dumps({"tools": [], "content": draft})
                agent._ask_with_stop = mock.Mock(return_value={"role": "assistant", "content": response})
                tasks = queue.Queue()
                tasks.put("你好")
                tasks.put(None)
                with mock.patch("mmclaw.kernel.persist_model_call"), mock.patch(
                    "mmclaw.config.ConfigManager.get_full_prompt", return_value="prompt"
                ):
                    agent._worker(tasks, "chat")
                agent.connector.send.assert_called_once_with("这是经过证据校验后实际发出的答复")
                saved = Path(agent.memory.session_file).read_text()
                self.assertNotIn("REJECTED_DRAFT", saved)
                self.assertIn("这是经过证据校验后实际发出的答复", saved)
                prompt = agent._ask_with_stop.call_args.args[0][0]["content"]
                self.assertIn("[CURRENT FINANCE TURN]", prompt)
                self.assertIn("historical", prompt)


class ToolFailureAndResolutionTests(unittest.TestCase):
    def test_blocked_json_is_a_failure_even_with_rows_and_evidence(self):
        result = json.dumps({"meta": {"status": "blocked", "row_count": 221}, "evidence": [{}]})
        self.assertEqual(ToolLoopController.result_error_key("finance_research_bundle", result),
                         "finance_research_bundle:status=blocked")
        self.assertIsNone(ToolLoopController.result_error_key("finance_research_bundle", json.dumps({
            "meta": {"status": "ok", "quality_status": "warn"}
        })))

    def test_equity_resolves_through_stock_filter(self):
        conn = mock.MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        with mock.patch("mmclaw.finance_db_helper.connect") as connect, contextlib.redirect_stdout(io.StringIO()):
            connect.return_value.__enter__.return_value = conn
            run_entity_resolve("", {"query": "长鑫科技", "asset_type": "equity"})
        self.assertEqual(conn.execute.call_args.args[1]["asset_type"], "stock")


class CurrencyEvidenceTests(unittest.TestCase):
    def make_store(self):
        store = EvidenceStore("f" * 32, "分析公司营收")
        store.items[NEW_ID] = {
            "evidence_id": NEW_ID, "claim_type": "actual", "metric_id": "revenue", "unit": "CNY",
            "value": {"value": "150310076758.00", "unit": "CNY", "yoy": "873.64"},
        }
        return store

    def test_currency_formatting_keeps_supported_financial_amounts(self):
        store = self.make_store()
        for amount in ("1503.10亿元", "15,031,007.68万元", "150,310,076,758元", "**1503.10**亿元"):
            with self.subTest(amount=amount):
                self.assertTrue(store.validate(f"营业收入{amount} [{NEW_ID}]")["passed"])

    def test_wrong_unit_wrong_rounding_and_sign_do_not_pass(self):
        store = self.make_store()
        for amount in ("1503.10万元", "1503.11亿元", "1503100767.58元", "-1503.10亿元"):
            with self.subTest(amount=amount):
                self.assertFalse(store.validate(f"营业收入{amount} [{NEW_ID}]")["passed"])

    def test_currency_requires_cited_typed_amount_not_yoy_metadata_or_foreign_currency(self):
        store = self.make_store()
        self.assertFalse(store.validate(f"营业收入873.64亿元 [{NEW_ID}]")["passed"])
        store.items[NEW_ID]["unit"] = "USD"
        store.items[NEW_ID]["value"]["unit"] = "USD"
        self.assertFalse(store.validate(f"营业收入1503.10亿元 [{NEW_ID}]")["passed"])
        store = self.make_store()
        self.assertFalse(store.validate(f"营业收入1503.10亿元 [{OLD_ID}]")["passed"])

    def test_cninfo_source_name_is_not_china_securities_journal(self):
        store = self.make_store()
        store.items[NEW_ID]["source_id"] = "cninfo_official"
        self.assertFalse(store.validate(f"财务数据来自中证网 [{NEW_ID}]")["passed"])
        self.assertTrue(store.validate(f"财务数据来自巨潮资讯（公司公告） [{NEW_ID}]")["passed"])


if __name__ == "__main__":
    unittest.main()
