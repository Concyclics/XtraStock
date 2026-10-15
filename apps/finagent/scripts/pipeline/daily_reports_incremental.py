#!/usr/bin/env python3
from __future__ import annotations

import sys

import argparse
import datetime as dt

from common import crawler_run, iso, log_path, max_report_date, run_logged, today


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Eastmoney research-report incremental crawl and import.")
    parser.add_argument("--start-date", help="Override start date YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Override end date YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--skip-ocr-discover", action="store_true")
    args = parser.parse_args()

    end = dt.date.fromisoformat(args.end_date) if args.end_date else today()
    if args.start_date:
        start = dt.date.fromisoformat(args.start_date)
    else:
        latest = max_report_date()
        start = (latest or end) - dt.timedelta(days=args.lookback_days)
    if start > end:
        start = end

    log_file = log_path("eastmoney_reports")
    with crawler_run("daily_reports", "eastmoney", start, end, log_file):
        run_logged(
            [
                sys.executable,
                "-u",
                "eastmoney_reports_incremental.py",
                "--storage",
                "sqlite",
                "--db-file",
                "eastmoney_reports.sqlite3",
                "--start-date",
                iso(start),
                "--end-date",
                iso(end),
                "--sleep",
                str(args.sleep),
            ],
            log_file,
        )
        if not args.skip_import:
            run_logged(
                [
                    sys.executable,
                    "scripts/pipeline/incremental_import.py",
                    "--source", "eastmoney",
                ],
                log_file,
            )
        if not args.skip_ocr_discover:
            run_logged([sys.executable, "scripts/pipeline/ocr_discover_queue.py"], log_file)

    print(f"[daily_reports] complete {iso(start)}..{iso(end)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
