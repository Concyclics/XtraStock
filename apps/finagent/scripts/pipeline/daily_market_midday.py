#!/usr/bin/env python3
from __future__ import annotations

import sys

import argparse

from common import crawler_run, log_path, run_logged, today


def main() -> int:
    parser = argparse.ArgumentParser(description="Run midday market incremental crawl.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    log_file = log_path("market_midday")
    cmd = [
        sys.executable,
        "-u",
        "Spider/eastmoney/scripts/crawl_daily_pg.py",
        "--universe",
        "--state-job",
        "daily_midday",
        "--asset-type",
        "stock",
        "--asset-type",
        "index",
        "--no-ticks",
        "--no-orderbook",
        "--no-events",
        "--allow-kline-failure",
        "--no-kline-probe",
        "--skip-daily-kline",
        "--workers",
        str(args.workers),
    ]
    if args.limit:
        cmd.extend(["--limit", str(args.limit)])

    with crawler_run("daily_market_midday", "eastmoney_market", today(), today(), log_file):
        run_logged(cmd, log_file)
    print("[daily_market_midday] complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
