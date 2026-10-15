import json
import datetime as dt
import os
import unittest
from unittest import mock

from mmclaw.evidence import EvidenceStore
from mmclaw.providers.openai_compatible import OpenAICompatibleProvider
from mmclaw.tool_router import PUBLIC_FINANCE_V3_TOOLS, route_finance_tools
from mmclaw.tool_schemas import get_native_tool_schemas
from mmclaw.tools import FinanceDBTool


class FinanceToolRouterV3Tests(unittest.TestCase):
    def test_compound_route_is_bounded_and_public(self):
        tools = route_finance_tools("分析AI硬件板块、财报、新闻、因子和走势图")
        self.assertLessEqual(len(tools), 10)
        self.assertTrue(set(tools).issubset(set(PUBLIC_FINANCE_V3_TOOLS)))
        self.assertIn("finance_graph_analysis", tools)
        self.assertIn("finance_fundamentals_batch", tools)
        self.assertIn("finance_document_retrieve", tools)
        self.assertIn("finance_factor_analysis", tools)

    def test_unknown_route_does_not_expose_raw_sql(self):
        tools = route_finance_tools("帮我看看这个问题")
        self.assertIn("finance_research_bundle", tools)
        self.assertNotIn("finance_db_query", tools)

    def test_native_schema_uses_dynamic_route(self):
        config = {"tool_profile": "finance_chat_readonly", "browser": {"enabled": False}}
        schemas = get_native_tool_schemas(config, context_text="最近一周股价下跌超过20%的股票")
        names = {item["function"]["name"] for item in schemas}
        self.assertIn("finance_time_context", names)
        self.assertIn("finance_market_screen", names)
        self.assertIn("finance_market_series_batch", names)
        self.assertNotIn("finance_db_query", names)
        self.assertLessEqual(len(names), 10)

    def test_time_context_schema_timezone_alias_is_accepted(self):
        with mock.patch.object(FinanceDBTool, "_v3_child", return_value='{"meta":{"status":"ok"}}') as child:
            result = FinanceDBTool.time_context(timezone="Asia/Shanghai")
        self.assertIn('"status":"ok"', result)
        self.assertEqual("Asia/Shanghai", child.call_args.args[1]["timezone"])


class EvidenceStoreV3Tests(unittest.TestCase):
    def make_store(self):
        store = EvidenceStore("0123456789abcdef0123456789abcdef", "今天股价是多少")
        payload = {
            "meta": {"status": "ok", "tool_version": "finance-agent:v3.0"},
            "rows": [],
            "evidence": [{
                "evidence_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001",
                "claim_type": "market",
                "entity_id": "000001",
                "metric_id": "close",
                "value": {"close": 12.34, "return_pct": -0.052},
                "unit": "CNY",
                "as_of_date": "2026-08-10",
                "available_at": "2026-08-10T17:05:00+08:00",
                "source_id": "eastmoney:kline:day:none",
                "quality_status": "verified",
            }],
        }
        self.assertEqual(store.add_tool_result("finance_market_series_batch", json.dumps(payload)), 1)
        return store

    def test_valid_answer_passes(self):
        store = self.make_store()
        answer = "截至2026-08-10，收盘价为12.34元 [aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。"
        self.assertTrue(store.validate(answer)["passed"])

    def test_unknown_evidence_and_number_fail(self):
        store = self.make_store()
        answer = "截至2026-08-10，收盘价为99.9元 [bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:E0001]。"
        result = store.validate(answer)
        self.assertFalse(result["passed"])
        codes = {item["code"] for item in result["violations"]}
        self.assertIn("unknown_evidence_id", codes)
        self.assertIn("unsupported_numeric_value", codes)

    def test_relative_date_requires_absolute_date(self):
        store = self.make_store()
        answer = "今天收盘价为12.34元 [aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。"
        result = store.validate(answer)
        self.assertFalse(result["passed"])
        self.assertIn("relative_date_without_absolute_date", {item["code"] for item in result["violations"]})

    def test_failed_tool_is_not_evidence(self):
        store = EvidenceStore("0123456789abcdef0123456789abcdef", "公司利润多少")
        store.add_tool_result("finance_fundamentals_batch", "FinanceDB error: timeout")
        result = store.validate("公司净利润增长50%。")
        self.assertFalse(result["passed"])
        self.assertIn("numeric_claim_without_evidence", {item["code"] for item in result["violations"]})

    def test_wrong_return_direction_is_rejected(self):
        store = self.make_store()
        store.items["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001"]["value"]["return_pct"] = 0.2138
        result = store.validate(
            "截至2026-08-10，股票上涨-21.38% "
            "[aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。"
        )
        self.assertFalse(result["passed"])
        self.assertIn("unsupported_numeric_value", {item["code"] for item in result["violations"]})

    def test_hallucinated_stock_code_is_rejected(self):
        store = self.make_store()
        store.items["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001"]["value"]["stock_code"] = "300394"
        result = store.validate(
            "截至2026-08-10，比亚迪002594收盘价为12.34元 "
            "[aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。"
        )
        self.assertFalse(result["passed"])
        self.assertIn("unsupported_stock_code", {item["code"] for item in result["violations"]})

    def test_leading_zero_stock_code_is_not_a_numeric_metric(self):
        store = self.make_store()
        store.items["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001"]["value"]["stock_code"] = "002371"
        result = store.validate("截至2026-08-10，公司代码002371 [aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。")
        self.assertTrue(result["passed"], result)

    def test_unknown_leading_zero_stock_code_remains_rejected(self):
        store = self.make_store()
        result = store.validate("截至2026-08-10，公司代码002371 [aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。")
        self.assertFalse(result["passed"])
        self.assertIn("unsupported_stock_code", {item["code"] for item in result["violations"]})

    def test_grouped_financial_number_matches_full_value(self):
        store = self.make_store()
        store.items["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001"]["value"] = 150310076758.00
        result = store.validate("截至2026-08-10，营业收入150,310,076,758.00元 [aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。")
        self.assertTrue(result["passed"], result)
        wrong = store.validate("截至2026-08-10，营业收入250,310,076,758.00元 [aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。")
        self.assertFalse(wrong["passed"])

    def test_timestamp_components_are_not_financial_numbers(self):
        store = self.make_store()
        result = store.validate("截至2026-08-10，记录时间2026-08-10T16:00:00+08:00，收盘价12.34元 [aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。")
        self.assertTrue(result["passed"], result)
        wrong = store.validate("截至2026-08-10 16:00:00，收盘价99.90元 [aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:E0001]。")
        self.assertFalse(wrong["passed"])

    def test_repair_budget_preserves_latest_prices_and_later_financial_tool(self):
        store = self.make_store()
        for n in range(1, 51):
            key = f"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:E{n:04d}"
            store.items[key] = {
                "evidence_id": key, "claim_type": "market", "tool_name": "finance_market_series_batch",
                "as_of_date": (dt.date(2026, 7, 26) + dt.timedelta(days=n)).isoformat(),
                "value": {"close": n, "padding": "x" * 600},
            }
        key = "cccccccccccccccccccccccccccccccc:E0001"
        for n in range(1, 21):
            news_key = f"dddddddddddddddddddddddddddddddd:E{n:04d}"
            store.items[news_key] = {
                "evidence_id": news_key, "claim_type": "document_claim", "metric_id": "document",
                "tool_name": "finance_research_bundle", "as_of_date": "2026-10-12",
                "value": {"snippet": "news " * 200},
            }
        store.items[key] = {
            "evidence_id": key, "claim_type": "actual", "tool_name": "finance_research_bundle",
            "period_end": "2026-06-30", "value": {"revenue": 150310076758.00},
        }
        prompt = store.repair_prompt("需要核对数据", {"violations": []})
        ledger = json.loads(prompt.split("Evidence ledger:\n", 1)[1])
        ids = {item["evidence_id"] for item in ledger}
        self.assertIn(key, ids)
        self.assertIn("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:E0050", ids)
        self.assertLessEqual(len(json.dumps(ledger, ensure_ascii=False)), 18000)


class OpenAICompatibleProviderV3Tests(unittest.TestCase):
    def test_api_key_env_and_reasoning_effort_are_applied(self):
        config = {
            "engine_type": "test_openai_compatible",
            "stream": False,
            "engines": {
                "test_openai_compatible": {
                    "api_key_env": "MMCLAW_TEST_PROVIDER_KEY",
                    "base_url": "https://provider.invalid/v1/",
                    "model": "gpt-test",
                    "reasoning_effort": "high",
                }
            },
        }
        captured = {}
        with mock.patch.dict(os.environ, {"MMCLAW_TEST_PROVIDER_KEY": "secret-from-env"}):
            provider = OpenAICompatibleProvider(config)
            provider._ask_blocking = lambda url, payload: captured.update(
                {"url": url, "payload": payload}
            ) or {"role": "assistant", "content": "ok"}
            result = provider.ask_once([{"role": "user", "content": "ping"}])

        self.assertEqual("secret-from-env", provider.api_key)
        self.assertEqual("https://provider.invalid/v1/chat/completions", captured["url"])
        self.assertEqual("high", captured["payload"]["reasoning_effort"])
        self.assertEqual("gpt-test", captured["payload"]["model"])
        self.assertEqual("ok", result["content"])

    def test_responses_mode_uses_plain_text_and_disables_native_tools(self):
        config = {
            "engine_type": "test_responses",
            "stream": False,
            "engines": {
                "test_responses": {
                    "api_key": "test-key",
                    "base_url": "https://provider.invalid/v1",
                    "model": "gpt-test",
                    "api_mode": "responses",
                }
            },
        }
        captured = {}
        provider = OpenAICompatibleProvider(config)
        provider._ask_responses = lambda url, payload: captured.update(
            {"url": url, "payload": payload}
        ) or {"role": "assistant", "content": "ok"}
        result = provider.ask_once([
            {"role": "system", "content": "system contract"},
            {"role": "user", "content": "ping"},
        ])

        self.assertFalse(provider.supports_native_tools)
        self.assertEqual("https://provider.invalid/v1/responses", captured["url"])
        self.assertIsInstance(captured["payload"]["input"], str)
        self.assertIn("[SYSTEM]", captured["payload"]["input"])
        self.assertIn("[USER]", captured["payload"]["input"])
        self.assertNotIn("tools", captured["payload"])
        self.assertEqual("ok", result["content"])


if __name__ == "__main__":
    unittest.main()
