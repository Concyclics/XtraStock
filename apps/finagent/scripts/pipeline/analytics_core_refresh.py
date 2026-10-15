#!/usr/bin/env python3
"""Refresh production analytics and factor layers after daily report ingestion."""
from __future__ import annotations

import sys

import argparse
import datetime as dt

from common import connect, crawler_run, iso, log_path, run_logged, today, pipeline_lock
from market_data_date import complete_market_date


CORE_SCHEMAS = (
    "docker/postgres/schema_analytics_signals.sql",
    "docker/postgres/schema_finance_fact_checks.sql",
    "docker/postgres/schema_agent_knowledge.sql",
    "docker/postgres/schema_point_in_time.sql",
    "docker/postgres/schema_factor_research.sql",
    "docker/postgres/schema_agent_portfolio.sql",
)


def reports_ready(as_of: dt.date) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT status
            FROM ops.crawler_runs
            WHERE task_name = 'daily_reports'
              AND end_date = %s
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (as_of,),
        ).fetchone()
    return bool(row and row["status"] == "ok")


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh daily analytics and production factor layers.")
    parser.add_argument("--date", help="Analysis date YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--apply-schema", action="store_true", help="Apply core schemas before refresh; reserved for maintenance runs.")
    parser.add_argument("--skip-technical", action="store_true")
    parser.add_argument("--ignore-report-gate", action="store_true")
    parser.add_argument("--research-lookback-days", type=int, default=14)
    parser.add_argument("--forecast-lookback-days", type=int, default=180)
    args = parser.parse_args()

    as_of = dt.date.fromisoformat(args.date) if args.date else today()
    requested_as_of = as_of
    with connect() as conn:
        as_of, market_status = complete_market_date(conn, requested_as_of)
    print(f"[analytics_core] data_date_gate={market_status}", flush=True)
    research_since = as_of - dt.timedelta(days=args.research_lookback_days)
    label_since = as_of - dt.timedelta(days=180)
    log_file = log_path("analytics_core")

    with pipeline_lock("analytics_core"), crawler_run("daily_analytics_core", "analytics", research_since, as_of, log_file):
        if not args.ignore_report_gate and not reports_ready(requested_as_of):
            raise RuntimeError(f"daily_reports has not succeeded for {iso(as_of)}")

        if args.apply_schema:
            for sql_path in CORE_SCHEMAS:
                run_logged(
                    [sys.executable, "scripts/analytics/apply_schema.py", "--admin", "--sql", sql_path],
                    log_file,
                )

        run_logged(
            [
                sys.executable, "scripts/analytics/refresh_point_in_time_metadata.py",
                "--as-of", iso(as_of), "--skip-financial-backfill",
            ],
            log_file,
        )
        run_logged(
            [
                sys.executable, "scripts/analytics/build_asset_forecast_daily.py",
                "--as-of", iso(as_of), "--lookback-days", str(args.forecast_lookback_days),
            ],
            log_file,
        )
        run_logged(
            [
                sys.executable, "scripts/analytics/build_research_signals.py",
                "--since", iso(research_since), "--until", iso(as_of),
            ],
            log_file,
        )
        if not args.skip_technical:
            run_logged(
                [
                    sys.executable, "scripts/analytics/build_technical_signals.py",
                    "--since", iso(research_since), "--until", iso(as_of),
                ],
                log_file,
            )
        run_logged(
            [
                sys.executable, "scripts/analytics/build_fusion_signals.py",
                "--since", iso(research_since), "--until", iso(as_of),
            ],
            log_file,
        )
        run_logged(
            [sys.executable, "scripts/analytics/build_asset_snapshot_daily.py", "--as-of", iso(as_of)],
            log_file,
        )

        if not args.skip_technical:
            run_logged(
                [
                    sys.executable, "scripts/analytics/build_factor_features.py",
                    "--since", iso(label_since), "--until", iso(as_of),
                    "--chunk-days", "31", "--labels-only",
                ],
                log_file,
            )

    print(f"[analytics_core] complete as_of={iso(as_of)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
