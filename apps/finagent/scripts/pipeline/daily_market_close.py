#!/usr/bin/env python3
from __future__ import annotations

import sys

import argparse
import datetime as dt
import subprocess

from common import crawler_run, log_path, pipeline_lock, run_logged, today


def main() -> int:
    parser = argparse.ArgumentParser(description="Run close-time market incremental crawl.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--asset-type", action="append", choices=["stock", "index", "industry", "concept", "board"])
    parser.add_argument("--with-ticks", action="store_true")
    parser.add_argument("--with-orderbook", action="store_true")
    parser.add_argument("--with-events", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--date", help="Override crawl cursor date YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--no-sina-fallback", action="store_true", help="Disable Sina daily K-line fallback/backfill.")
    parser.add_argument("--primary-budget-seconds", type=int, default=300)
    args = parser.parse_args()

    log_file = log_path("market_close")
    run_date = args.date or today().isoformat()
    asset_types = args.asset_type or ["stock", "index"]
    cmd = [
        sys.executable,
        "-u",
        "Spider/eastmoney/scripts/crawl_daily_pg.py",
        "--universe",
        "--state-job",
        "daily_close",
        "--workers",
        str(args.workers),
        "--max-seconds", str(max(30, args.primary_budget_seconds)),
        "--health-output", str(log_file.with_suffix(".sources.json")),
    ]
    if args.force:
        cmd.append("--force")
    if args.date:
        cmd.extend(["--cursor-date", args.date])
    for asset_type in asset_types:
        cmd.extend(["--asset-type", asset_type])
    if not args.with_ticks:
        cmd.append("--no-ticks")
    if not args.with_orderbook:
        cmd.append("--no-orderbook")
    if not args.with_events:
        cmd.append("--no-events")
    if args.limit:
        cmd.extend(["--limit", str(args.limit)])

    fallback_cmd = [
        sys.executable,
        "-u",
        "scripts/pipeline/market_kline_sina_backfill.py",
        "--start",
        run_date,
        "--end",
        run_date,
        "--asset-type",
        "stock",
        "--workers",
        str(min(args.workers, 4)),
        "--summary-output",str(log_file.with_suffix(".fallback.json")),
        "--success-state-job",
        "daily_close",
    ]
    if args.limit:
        fallback_cmd.extend(["--limit", str(args.limit)])
    if "index" in asset_types:
        fallback_cmd.extend(["--asset-type", "index"])

    canonical_cmd = [
        sys.executable,
        "-u",
        "scripts/analytics/sync_canonical_daily_bars.py",
        "--since",
        run_date,
        "--until",
        run_date,
        "--asset-types",
        ",".join(asset_types),
    ]
    if not args.limit and "stock" in asset_types:
        canonical_cmd.extend(["--min-assets", "3000"])

    canonical_v2_cmd = [
        sys.executable, "-u", "scripts/analytics/sync_canonical_daily_bars_v2.py",
        "--since", run_date, "--until", run_date,
    ]
    adjustment_v2_cmd = [
        sys.executable, "-u", "scripts/analytics/build_adjustment_factors_v3.py",
        "--since", run_date, "--until", run_date,
    ]

    if args.limit:
        canonical_v2_cmd.append("--allow-quality-fail")
        adjustment_v2_cmd.append("--allow-quality-fail")
    run_day = dt.date.fromisoformat(run_date)
    with pipeline_lock("market_close"), crawler_run("daily_market_close", "eastmoney_market", run_day, run_day, log_file):
        eastmoney_error = None
        try:
            run_logged(cmd, log_file)
        except subprocess.CalledProcessError as exc:
            eastmoney_error = exc
            print(f"[daily_market_close] eastmoney crawl failed, trying Sina daily fallback: {exc}")
        if eastmoney_error is not None:
            if args.no_sina_fallback or "stock" not in asset_types:
                raise eastmoney_error
            try:
                run_logged(fallback_cmd, log_file)
            except subprocess.CalledProcessError as fallback_error:
                if fallback_error.returncode != 2:
                    raise
                print("[daily_market_close] Sina partial failures (exit=2); saved details and canonical gate determine completeness",flush=True)
        run_logged(canonical_cmd, log_file)
        if "stock" in asset_types:
            run_logged([sys.executable,"scripts/analytics/sync_asset_lifecycle_v3.py","--as-of",run_date],log_file)
            run_logged(canonical_v2_cmd, log_file)
            run_logged(adjustment_v2_cmd, log_file)
    print("[daily_market_close] complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
