"""Shared morning gate/report freshness checks, using market sessions for prices."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


MARKET_DAY_DATASETS = {
    "market.canonical_bars_daily_v2", "market.asset_lifecycle_versions",
    "market.adjustment_factors_daily", "factor.daily_scores", "kg.temporal_graph",
}


def freshness_issue(key, row, run_date, market_date):
    if key == "market.trading_calendar":
        # A verified annual schedule can already cover the rest of the year.
        # expected_date is its sync date, not the end of its calendar coverage.
        through = row.get("data_through")
        if isinstance(through, dt.datetime):
            through = through.astimezone(ZoneInfo("Asia/Shanghai")).date()
        if through is None or through < run_date:
            return {"reason": "calendar_coverage_gap", "data_through": str(through),
                    "required_date": run_date.isoformat()}
        return None
    if key in MARKET_DAY_DATASETS:
        if market_date is None:
            return {"reason": "missing_previous_market_date"}
        required = market_date
    else:
        required = run_date if key == "agent.daily_data_gate" else run_date - dt.timedelta(days=1)
    expected = row.get("expected_date")
    if expected is None or expected < required:
        return {"reason": "missing_expected_date" if expected is None else "stale_expected_date",
                "expected_date": expected.isoformat() if expected else None,
                "required_date": required.isoformat()}
    return None
