from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "scripts/pipeline"
ANALYTICS = ROOT / "scripts/analytics"
sys.path.insert(0, str(PIPELINE))
sys.path.append(str(ANALYTICS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


report_v3 = load_module("daily_ai_hardware_report_v3", PIPELINE / "daily_ai_hardware_report_v3.py")
enqueue_v3 = load_module("enqueue_daily_report_v3", PIPELINE / "enqueue_daily_report_v3.py")
cninfo_v3 = load_module("daily_cninfo_filings", PIPELINE / "daily_cninfo_filings.py")
corporate_actions_v3 = load_module(
    "extract_corporate_actions_cninfo", ANALYTICS / "extract_corporate_actions_cninfo.py"
)
financial_facts_v3 = load_module(
    "extract_financial_facts_cninfo", ANALYTICS / "extract_financial_facts_cninfo.py"
)
openai_client = load_module(
    "openai_compatible_client", PIPELINE / "openai_compatible_client.py"
)


class ReportClaimsTests(unittest.TestCase):
    def snapshot(self):
        return {
            "report_date": dt.date(2026, 8, 10),
            "decision_cutoff": dt.datetime(2026, 8, 10, 8, 50, tzinfo=dt.timezone(dt.timedelta(hours=8))),
            "market_date": dt.date(2026, 8, 7),
            "hard_gate_passed": True,
            "hard_gate_blockers": [],
            "evidence": [{
                "evidence_id": "MKT0001", "claim_type": "market", "entity_id": "300394",
                "metric_id": "total_return_close", "value": {"stock_code": "300394", "close": "216.85"},
                "as_of_date": "2026-08-07", "available_at": "2026-08-07T15:05:00+08:00",
            }],
        }

    def test_claim_requires_bound_number(self):
        claims = [{
            "claim_id": "C001", "section_name": "今日核心判断", "claim_type": "market",
            "claim_text": "300394 收盘 999.00 元", "evidence_ids": ["MKT0001"],
        }]
        checked, errors = report_v3.validate_claims(claims, self.snapshot())
        self.assertEqual("rejected", checked[0]["validation_status"])
        self.assertTrue(any("unsupported numbers" in item for item in errors))

    def test_claim_accepts_bound_number_and_code(self):
        claims = [{
            "claim_id": "C001", "section_name": "关键证据", "claim_type": "market",
            "claim_text": "300394 收盘 216.85 元", "evidence_ids": ["MKT0001"],
        }]
        checked, errors = report_v3.validate_claims(claims, self.snapshot())
        self.assertFalse(errors)
        self.assertEqual("verified", checked[0]["validation_status"])

    def test_degraded_report_has_no_recommendation(self):
        snapshot = self.snapshot()
        snapshot["hard_gate_passed"] = False
        snapshot["hard_gate_blockers"] = ["failed:market"]
        claims = report_v3.degraded_claims(snapshot)
        self.assertNotIn("今日投资建议", {item["section_name"] for item in claims})

    def test_generation_failure_keeps_exception_internal_and_does_not_invent_a_data_gap(self):
        claims = report_v3.degraded_claims(self.snapshot(), "TimeoutError private/path request_id=abcdef123456")
        text = "\n".join(claim["claim_text"] for claim in claims)
        self.assertIn("未能完成生成或通过核验", text)
        self.assertNotIn("数据硬门禁未通过", text)
        self.assertNotIn("private/path", text)
        self.assertNotIn("abcdef123456", text)

    def test_inline_snapshot_is_bounded_and_preserves_evidence_ids(self):
        snapshot = self.snapshot()
        snapshot["evidence"][0]["value"]["long_text"] = "证据" * 5000
        payload = report_v3.inline_json(report_v3.compact_v3_snapshot(snapshot))
        self.assertIn("MKT0001", payload)
        self.assertLessEqual(len(payload), 200_000)

    def test_compact_snapshot_keeps_each_metric_with_limits(self):
        snapshot = self.snapshot()
        snapshot["evidence"] = [
            {
                "evidence_id": f"DOC{index:04d}",
                "claim_type": "document_claim",
                "metric_id": "published_document",
                "value": {"body": "A" * 3000},
            }
            for index in range(50)
        ] + [
            {
                "evidence_id": f"FAC{index:04d}",
                "claim_type": "model_signal",
                "metric_id": "factor_x",
                "value": index,
            }
            for index in range(50)
        ]
        compact = report_v3.compact_v3_snapshot(snapshot)
        self.assertEqual(42, len(compact["evidence"]))
        self.assertEqual(100, compact["llm_payload_metadata"]["original_evidence_count"])

    def test_validator_normalizes_section_percent_and_date_formats(self):
        snapshot = self.snapshot()
        snapshot["evidence"][0]["value"] = {
            "coverage_pct": 100,
            "published_on": "2026-08-04",
        }
        claims = [{
            "claim_id": "C001",
            "section_name": "数据质量与时效",
            "claim_type": "data_quality",
            "claim_text": "2026年8月4日覆盖率为100.0%",
            "evidence_ids": ["MKT0001"],
        }]
        checked, errors = report_v3.validate_claims(claims, snapshot)
        self.assertFalse(errors)
        self.assertEqual("数据质量与限制", checked[0]["section_name"])
        self.assertEqual("verified", checked[0]["validation_status"])

    def test_timestamp_digits_do_not_authorize_unrelated_zero(self):
        snapshot = self.snapshot()
        claims = [{
            "claim_id": "C001",
            "section_name": "数据质量与限制",
            "claim_type": "data_quality",
            "claim_text": "冲突记录为0条",
            "evidence_ids": ["MKT0001"],
        }]
        checked, errors = report_v3.validate_claims(claims, snapshot)
        self.assertTrue(errors)
        self.assertEqual("rejected", checked[0]["validation_status"])


class OpenAICompatibleClientTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, *, lines=None, body=b""):
            self.lines = lines or []
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter(self.lines)

        def read(self):
            return self.body

    def test_stream_error_falls_back_to_blocking(self):
        stream_error = self.FakeResponse(lines=[
            b"event: error\n",
            b'data: {"error":{"type":"upstream_error","message":"temporary"}}\n',
            b"\n",
        ])
        blocking_ok = self.FakeResponse(body=json.dumps({
            "choices": [{"message": {"content": '{"ok":true}'}}],
        }).encode())
        calls = []

        def fake_urlopen(request, timeout):
            calls.append(json.loads(request.data.decode()))
            return [stream_error, blocking_ok][len(calls) - 1]

        with mock.patch.dict(os.environ, {"TEST_API_KEY": "secret"}), \
             mock.patch.object(openai_client.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = openai_client.complete(
                [{"role": "user", "content": "test"}], model="model",
                base_url="https://provider.invalid/v1", api_key_env="TEST_API_KEY",
                stream=True, retries=0,
            )
        self.assertEqual('{"ok":true}', result)
        self.assertTrue(calls[0]["stream"])
        self.assertFalse(calls[1]["stream"])

    def test_responses_mode_flattens_messages_to_string(self):
        response = self.FakeResponse(body=json.dumps({
            "output": [{
                "type": "message",
                "content": [{"type": "output_text", "text": '{"ok":true}'}],
            }],
        }).encode())
        captured = []

        def fake_urlopen(request, timeout):
            captured.append(json.loads(request.data.decode()))
            return response

        with mock.patch.dict(os.environ, {"TEST_API_KEY": "secret"}), \
             mock.patch.object(openai_client.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = openai_client.complete(
                [{"role": "system", "content": "contract"}, {"role": "user", "content": "test"}],
                model="model", base_url="https://provider.invalid/v1",
                api_key_env="TEST_API_KEY", api_mode="responses",
                json_object=True, stream=False, retries=0,
            )

        self.assertEqual('{"ok":true}', result)
        self.assertIsInstance(captured[0]["input"], str)
        self.assertIn("[SYSTEM]", captured[0]["input"])
        self.assertIn("[OUTPUT CONTRACT]", captured[0]["input"])
        self.assertNotIn("text", captured[0])


class OutboxChunkTests(unittest.TestCase):
    def test_split_preserves_full_text(self):
        source = ("第一段\n" * 800) + ("X" * 9000)
        chunks = enqueue_v3.split_text(source, 1000)
        self.assertEqual(source, "".join(chunks))
        self.assertTrue(all(len(item) <= 1000 for item in chunks))

class CninfoPaginationTests(unittest.TestCase):
    def test_large_range_is_split_before_api_page_cap(self):
        calls = []

        def fake_fetch_page(_session, start, end, page, page_size, _search_key=""):
            calls.append((start, end, page, page_size))
            days = (end - start).days + 1
            total = days * 400
            start_index = (page - 1) * page_size
            rows = [
                {"announcementId": f"{start.isoformat()}-{i}"}
                for i in range(start_index, min(start_index + page_size, total))
            ]
            return {"totalAnnouncement": total, "announcements": rows}

        with mock.patch.object(cninfo_v3, "fetch_page", side_effect=fake_fetch_page), \
             mock.patch.object(cninfo_v3.time, "sleep", return_value=None):
            total, rows, segments = cninfo_v3.fetch_range(
                object(), dt.date(2026, 8, 1), dt.date(2026, 8, 10), 30, 1000
            )

        self.assertEqual(4000, total)
        self.assertEqual(4000, len(rows))
        self.assertGreater(len(segments), 1)
        self.assertTrue(all(not item["truncated_by_page_limit"] for item in segments))
        self.assertTrue(all(page <= cninfo_v3.CNINFO_PAGE_LIMIT for _, _, page, _ in calls))


class CorporateActionParserTests(unittest.TestCase):
    def test_official_distribution_plan_uses_per_share_units(self):
        parsed = corporate_actions_v3.parse_distribution(
            "本次利润分配每10股派发现金红利3.50元（含税），每10股转增2股。"
            "股权登记日：2026年8月13日，除权除息日：2026年8月14日，"
            "现金红利发放日：2026年8月14日。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(dt.date(2026, 8, 14), parsed["ex_date"])
        self.assertEqual(0.35, parsed["cash_dividend"])
        self.assertEqual(0.2, parsed["split_ratio"])
        self.assertEqual("split", parsed["action_type"])

    def test_sse_per_share_table_format(self):
        parsed = corporate_actions_v3.parse_distribution(
            "重要内容提示：\nA股每股现金红利0.04元\n相关日期\n"
            "股份类别 股权登记日 最后交易日 除权（息）日 现金红利发放日\n"
            "A股 2026/8/13 - 2026/8/14 2026/8/14\n"
            "本次利润分配每股派发现金红利0.04元（含税）。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(dt.date(2026, 8, 13), parsed["record_date"])
        self.assertEqual(dt.date(2026, 8, 14), parsed["ex_date"])
        self.assertEqual(0.04, parsed["cash_dividend"])

    def test_sse_per_share_rmb_before_amount(self):
        parsed = corporate_actions_v3.parse_distribution(
            "A股每股现金红利人民币0.147元（含税）。\n"
            "股份类别 股权登记日 最后交易日 除权（息）日 现金红利发放日\n"
            "A股 2026/8/13 - 2026/8/14 2026/8/14"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(0.147, parsed["cash_dividend"])
        self.assertEqual(dt.date(2026, 8, 14), parsed["ex_date"])

    def test_szse_cash_without_dividend_word_and_wrapped_date(self):
        parsed = corporate_actions_v3.parse_distribution(
            "向全体股东每10股派现金0.75元人民币（含税）。\n"
            "股权登记日为：2026年8月11日；除权除息日为：\n2026年8月12日。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(0.075, parsed["cash_dividend"])
        self.assertEqual(dt.date(2026, 8, 12), parsed["ex_date"])

    def test_szse_split_with_wrapped_date(self):
        parsed = corporate_actions_v3.parse_distribution(
            "向全体股东每10股转增7股。股权登记日为：2020年3月18日，"
            "除权除息日为：\n2020年3月19日。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(0.7, parsed["split_ratio"])
        self.assertEqual(dt.date(2020, 3, 19), parsed["ex_date"])

    def test_szse_amount_wrapped_after_cash_prefix(self):
        parsed = corporate_actions_v3.parse_distribution(
            "向全体股东每10股派\n5.400000元人民币现金（含税）。"
            "股权登记日为：2020年4月15日，除权除息日为：2020年4月16日。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(0.54, parsed["cash_dividend"])
        self.assertEqual(dt.date(2020, 4, 16), parsed["ex_date"])

    def test_invalid_table_date_does_not_raise(self):
        parsed = corporate_actions_v3.parse_distribution(
            "每10股派现金红利1元。除权除息日：2020年4月30日。\n"
            "股权登记日 除权（息）日 现金红利发放日\n"
            "2020/4/31 2020/5/1 2020/5/1"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(dt.date(2020, 4, 30), parsed["ex_date"])

    def test_multiline_sse_table_with_listing_date(self):
        parsed = corporate_actions_v3.parse_distribution(
            "A股每股现金红利0.4元，每股转增股份0.4股。\n"
            "除权（息） 现金红利发放日\n"
            "股份类别 股权登记日 最后交易日 新增无限售条件流通股上市日\n"
            "A股 2021/4/15 - 2021/4/16 2021/4/19 2021/4/16"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(dt.date(2021, 4, 15), parsed["record_date"])
        self.assertEqual(dt.date(2021, 4, 16), parsed["ex_date"])
        self.assertEqual(dt.date(2021, 4, 16), parsed["pay_date"])
        self.assertEqual(0.4, parsed["split_ratio"])

    def test_cash_rmb_before_amount_and_parenthesized_ex_label(self):
        parsed = corporate_actions_v3.parse_distribution(
            "每10股派发现金股利人民币5.00元。"
            "股权登记日：2021年4月22日，除权日（除息日）：2021年4月23日。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(0.5, parsed["cash_dividend"])
        self.assertEqual(dt.date(2021, 4, 23), parsed["ex_date"])

    def test_adjacent_sse_table_dates_are_not_concatenated(self):
        parsed = corporate_actions_v3.parse_distribution(
            "A股每股现金红利0.25元。\n"
            "股权登记日 除权（息）日 现金红利发放日\n"
            "2021/4/30       2021/5/6       2021/5/6"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(dt.date(2021, 4, 30), parsed["record_date"])
        self.assertEqual(dt.date(2021, 5, 6), parsed["ex_date"])
        self.assertEqual(dt.date(2021, 5, 6), parsed["pay_date"])

    def test_cash_amount_before_rmb_cash_suffix(self):
        parsed = corporate_actions_v3.parse_distribution(
            "向全体股东每10股派发8.000000元人民币现金（含税）。"
            "股权登记日为：2021年4月28日，除权除息日为：2021年4月29日。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(0.8, parsed["cash_dividend"])

    def test_differential_distribution_uses_effective_ratios(self):
        parsed = corporate_actions_v3.parse_distribution(
            "差异化分红送转：是。每股现金红利0.2元，每股转增股份0.4股。"
            "虚拟分派的每股现金红利=(100*0.2)/102≈0.1960元/股。"
            "虚拟分派的流通股份变动比例=(100*0.4)/102=0.3920。"
            "股权登记日：2021/4/30，除权（息）日：2021/5/6。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(0.196, parsed["cash_dividend"])
        self.assertEqual(0.392, parsed["split_ratio"])
        self.assertEqual("verified", parsed["quality_status"])

    def test_explicit_b_share_is_excluded_from_a_share_coverage(self):
        self.assertTrue(corporate_actions_v3.is_explicit_non_a_share("200986", "粤华包Ｂ"))
        self.assertFalse(corporate_actions_v3.is_explicit_non_a_share("300866", "安克创新"))

    def test_sse_header_split_and_four_date_columns(self):
        parsed = corporate_actions_v3.parse_distribution(
            "A股每股现金红利0.132元，每股转增0.45股。\n"
            "股权登记 最后交易 除权（息） 新增无限售条件 现金红利发放\n"
            "日 日 日 流通股份上市日 日\n"
            "A股 2021/5/19 - 2021/5/20 2021/5/21 2021/5/20"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(dt.date(2021, 5, 20), parsed["ex_date"])
        self.assertEqual(dt.date(2021, 5, 20), parsed["pay_date"])

    def test_ex_date_without_repeated_year_uses_record_year(self):
        parsed = corporate_actions_v3.parse_distribution(
            "每10股派0.5元人民币现金。股权登记日为：2021年5月17日，"
            "除权除息日为5月18日。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(dt.date(2021, 5, 18), parsed["ex_date"])

    def test_malformed_ex_date_recovers_from_official_pay_date_as_unverified(self):
        parsed = corporate_actions_v3.parse_distribution(
            "每10股派0.5元人民币现金。股权登记日为：2021年6月2日，"
            "除权除息日为：2021年6月31日。"
            "现金红利将于2021年6月3日通过证券公司划入资金账户。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(dt.date(2021, 6, 3), parsed["ex_date"])
        self.assertTrue(parsed["ex_date_recovered_from_pay_date"])
        self.assertEqual("unverified", parsed["quality_status"])

    def test_stock_dividend_followed_by_cash_and_split(self):
        parsed = corporate_actions_v3.parse_distribution(
            "每10股送红股3.5股，派2.05元人民币现金，同时每10股转增0.5股。"
            "股权登记日：2021年5月20日，除权除息日：2021年5月21日。"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(0.205, parsed["cash_dividend"])
        self.assertEqual(0.35, parsed["stock_dividend_ratio"])
        self.assertEqual(0.05, parsed["split_ratio"])


class CninfoFilingClassificationTests(unittest.TestCase):
    def test_semiannual_report_is_not_classified_as_annual(self):
        self.assertEqual(
            "semiannual_report",
            cninfo_v3.filing_type("某公司2026年半年度报告"),
        )
        self.assertEqual(
            "annual_report",
            cninfo_v3.filing_type("某公司2025年年度报告"),
        )

    def test_report_related_notice_is_not_a_financial_filing(self):
        self.assertEqual(
            "announcement",
            cninfo_v3.filing_type("关于2026年半年度报告披露的提示性公告"),
        )
        self.assertEqual(
            "announcement",
            cninfo_v3.filing_type("关于2025年年度报告监管问询函的回复"),
        )
        self.assertEqual(
            "semiannual_report",
            cninfo_v3.filing_type("某公司2026年半年度报告摘要（修订版）"),
        )


class CninfoFinancialFactParserTests(unittest.TestCase):
    def test_parses_summary_values_units_and_yoy(self):
        facts = financial_facts_v3.parse_summary_facts(
            "主要会计数据\n单位：万元 币种：人民币\n本报告期 上年同期 同比增减\n"
            "营业收入 87,995.82 68,994.04 27.54\n"
            "归属于上市公司股东的净利润 6,805.53 4,557.35 49.33\n"
            "基本每股收益（元／股） 0.48 0.32 50.00\n"
        )
        by_code = {fact["metric_code"]: fact for fact in facts}
        self.assertEqual(Decimal("879958200.00"), by_code["revenue"]["value"])
        self.assertEqual(Decimal("68055300.00"), by_code["net_profit_parent"]["value"])
        self.assertEqual(Decimal("0.48"), by_code["eps_basic"]["value"])
        self.assertEqual(Decimal("27.54"), by_code["revenue"]["yoy"])

    def test_missing_currency_unit_does_not_invent_amount(self):
        facts = financial_facts_v3.parse_summary_facts(
            "营业收入 100 90 11.11\n基本每股收益（元／股） 0.1 0.08 25\n"
        )
        self.assertEqual([], facts)


class ScheduledProviderTests(unittest.TestCase):
    def test_openai_compatible_payload_uses_env_key_and_reasoning(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": '{"claims":[]}'}}]
                }).encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        with mock.patch.dict(os.environ, {"TEST_DAILY_KEY": "secret-value"}), \
             mock.patch.object(openai_client.urllib.request, "urlopen", side_effect=fake_urlopen):
            content = openai_client.complete(
                [{"role": "user", "content": "test"}], model="gpt-test",
                base_url="https://provider.invalid/v1/", api_key_env="TEST_DAILY_KEY",
                reasoning_effort="high", timeout=60, json_object=True,
            )

        self.assertEqual('{"claims":[]}', content)
        self.assertEqual("https://provider.invalid/v1/chat/completions", captured["url"])
        self.assertEqual("high", captured["payload"]["reasoning_effort"])
        self.assertEqual({"type": "json_object"}, captured["payload"]["response_format"])
        self.assertIn("secret-value", captured["headers"]["Authorization"])


if __name__ == "__main__":
    unittest.main()
