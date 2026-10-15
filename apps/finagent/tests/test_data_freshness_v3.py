import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/pipeline"))
from data_freshness_v3 import freshness_issue
from sync_trading_calendar_v3 import closed_dates


class FreshnessTests(unittest.TestCase):
    def test_monday_accepts_friday_adjustments_but_not_thursday(self):
        run, market = dt.date(2026, 10, 12), dt.date(2026, 10, 9)
        self.assertIsNone(freshness_issue("market.adjustment_factors_daily", {"expected_date": market}, run, market))
        issue = freshness_issue("market.adjustment_factors_daily", {"expected_date": dt.date(2026, 10, 8)}, run, market)
        self.assertEqual("2026-10-09", issue["required_date"])
        self.assertEqual("stale_expected_date", issue["reason"])

    def test_holiday_gap_uses_known_session_not_weekday_arithmetic(self):
        run, market = dt.date(2026, 10, 8), dt.date(2026, 9, 30)
        for key in ("market.canonical_bars_daily_v2", "market.adjustment_factors_daily", "factor.daily_scores"):
            self.assertIsNone(freshness_issue(key, {"expected_date": market}, run, market))

    def test_annual_calendar_uses_coverage_not_last_sync_day(self):
        row = {"expected_date": dt.date(2026, 8, 10), "data_through": dt.datetime(2026, 12, 31, 15, 59, tzinfo=dt.timezone.utc)}
        self.assertIsNone(freshness_issue("market.trading_calendar", row, dt.date(2026, 10, 12), None))
        self.assertEqual("calendar_coverage_gap", freshness_issue("market.trading_calendar", row, dt.date(2027, 1, 1), None)["reason"])

    def test_missing_dates_fail_closed(self):
        day = dt.date(2026, 10, 12)
        self.assertEqual("missing_previous_market_date", freshness_issue("market.adjustment_factors_daily", {"expected_date": day}, day, None)["reason"])
        self.assertEqual("missing_expected_date", freshness_issue("company.filings", {}, day, None)["reason"])
        self.assertEqual("calendar_coverage_gap", freshness_issue("market.trading_calendar", {}, day, None)["reason"])

    def test_calendar_day_sources_still_need_yesterday_and_gate_needs_today(self):
        day, friday = dt.date(2026, 10, 12), dt.date(2026, 10, 9)
        self.assertEqual("2026-10-11", freshness_issue("company.filings", {"expected_date": friday}, day, friday)["required_date"])
        self.assertEqual("2026-10-12", freshness_issue("agent.daily_data_gate", {"expected_date": day-dt.timedelta(days=1)}, day, friday)["required_date"])


class CalendarParserTests(unittest.TestCase):
    def test_annual_heading_does_not_hide_new_year_holiday(self):
        section = "2026年休市安排 元旦：1月1日（星期四）至1月3日（星期六）休市，1月5日（星期一）起照常开市。春节：2月15日至2月23日休市。"
        days = closed_dates(section, 2026)
        self.assertIn(dt.date(2026, 1, 1), days)
        self.assertIn(dt.date(2026, 1, 3), days)
        self.assertNotIn(dt.date(2026, 1, 5), days)
        self.assertIn(dt.date(2026, 2, 23), days)


if __name__ == "__main__":
    unittest.main()
