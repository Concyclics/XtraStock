from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/pipeline"))
SPEC = importlib.util.spec_from_file_location(
    "daily_ai_hardware_codex_agent",
    ROOT / "scripts/pipeline/daily_ai_hardware_codex_agent.py",
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DailyAgentTests(unittest.TestCase):
    def test_historical_decision_cutoff_is_publication_gate(self) -> None:
        cutoff = MODULE.decision_cutoff(dt.date(2020, 1, 2))
        self.assertEqual(cutoff, dt.datetime(2020, 1, 2, 9, 0, tzinfo=MODULE.TZ))

    def test_validate_report_ignores_uuid_numeric_fragments(self) -> None:
        report_date = dt.date(2026, 7, 17)
        market_date = dt.date(2026, 7, 16)
        sections = "\n".join(f"## {name}\n内容。" for name in MODULE.REPORT_SECTIONS)
        padding = "研究证据。" * 500
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            path.write_text(
                f"# AI硬件盘前日报 {report_date}\n\n"
                f"数据时点：{report_date} 09:00；行情日期：{market_date}。\n"
                "中际旭创（300308）。证据 de647446-be66-43b0-ad82-6133e114b71d。\n"
                f"{sections}\n{padding}\n",
                encoding="utf-8",
            )

            def fake_fetch_all(sql, params=()):
                self.assertEqual(params, (["300308"],))
                return [{"symbol": "300308"}]

            with patch.object(MODULE, "fetch_all", fake_fetch_all):
                result = MODULE.validate_report(path, report_date, market_date)
        self.assertEqual(result["stock_codes"], ["300308"])

    def test_decision_cutoff_wording_does_not_block_publication(self) -> None:
        report_date = dt.date(2026, 7, 22)
        market_date = dt.date(2026, 7, 21)
        sections = "\n".join(f"## {name}\n内容。" for name in MODULE.REPORT_SECTIONS)
        padding = "研究证据。" * 500
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            path.write_text(
                f"# AI硬件盘前日报 {report_date}\n\n"
                f"截至决策截止时间 {report_date} 08:52，行情日期：{market_date}。\n"
                f"{sections}\n{padding}\n",
                encoding="utf-8",
            )
            result = MODULE.validate_report(path, report_date, market_date)
        self.assertEqual(result["validation_status"], "passed")
        self.assertEqual(result["warnings"], [])

    def test_remote_model_cannot_claim_inline_pack_is_missing(self) -> None:
        report_date = dt.date(2026, 8, 11)
        market_date = dt.date(2026, 8, 10)
        sections = "\n".join(f"## {name}\n内容。" for name in MODULE.REPORT_SECTIONS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            path.write_text(
                f"# AI硬件盘前日报 {report_date}\n决策知识截止：2026-08-11 08:50\n"
                f"行情日期：{market_date}\n当前对话未提供 `data_pack.json` 的可读取内容。\n"
                f"{sections}\n" + "研究证据。" * 500,
                encoding="utf-8",
            )
            result = MODULE.validate_report(path, report_date, market_date)
        self.assertEqual("failed", result["validation_status"])
        self.assertTrue(result["fatal_errors"])

    def test_compact_remote_payload_is_bounded_and_keeps_same_day_data(self) -> None:
        pack = {
            "same_day_news": [{"title": str(index), "body": "A" * 3000} for index in range(40)],
            "factor_extremes": [{"value": index} for index in range(80)],
            "api_key": "must-not-leak",
        }
        compact = MODULE.compact_legacy_data_pack(pack)
        payload = MODULE.inline_json(compact)
        self.assertEqual(15, len(compact["same_day_news"]))
        self.assertEqual(24, len(compact["factor_extremes"]))
        self.assertNotIn("must-not-leak", payload)


if __name__ == "__main__":
    unittest.main()
