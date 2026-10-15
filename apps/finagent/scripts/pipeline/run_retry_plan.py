#!/usr/bin/env python3
from __future__ import annotations

import sys

import argparse

from common import ROOT, connect, ensure_ops_schema, latest_pending_retry_plan, log_path, run_logged


def update_plan_status(plan_id: int, status: str, result: dict | None = None) -> None:
    with connect(admin=False) as conn:
        conn.execute(
            """
            UPDATE ops.retry_plans
            SET status = %s,
                executed_at = CASE WHEN %s IN ('ok', 'failed', 'skipped') THEN now() ELSE executed_at END,
                result = COALESCE(%s::jsonb, result)
            WHERE id = %s
            """,
            (status, status, None if result is None else __import__("json").dumps(result, ensure_ascii=False), plan_id),
        )


def date_bounds(values: list[str]) -> tuple[str, str] | None:
    clean = sorted(v for v in values if v)
    if not clean:
        return None
    return clean[0], clean[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the newest pending retry plan.")
    parser.add_argument("--skip-market", action="store_true")
    parser.add_argument("--skip-reports", action="store_true")
    parser.add_argument("--skip-news", action="store_true")
    parser.add_argument("--include-market", action="store_true", help="Allow retry plan to rerun market close crawl.")
    args = parser.parse_args()

    ensure_ops_schema()
    pending = latest_pending_retry_plan()
    if not pending:
        print("[retry_plan] no pending retry plan")
        return 0

    plan_id = pending["id"]
    plan = pending["plan"]
    log_file = log_path("retry_plan")
    result: dict = {}
    update_plan_status(plan_id, "running")
    try:
        news_dates: list[str] = []
        if not args.skip_news:
            for source_plan in (plan.get("news") or {}).values():
                news_dates.extend(source_plan.get("missing_dates") or [])
                news_dates.extend(source_plan.get("missing_page_pdf_dates") or [])
            bounds = date_bounds(news_dates)
            if bounds:
                start, end = bounds
                run_logged(
                    [
                        sys.executable,
                        "scripts/pipeline/daily_news_incremental.py",
                        "--start",
                        start,
                        "--end",
                        end,
                    ],
                    log_file,
                )
                result["news"] = {"start": start, "end": end}

        reports_plan = plan.get("reports") or {}
        if not args.skip_reports and reports_plan.get("needs_import"):
            run_logged(
                [
                    sys.executable,
                    "scripts/pipeline/incremental_import.py",
                    "--source", "eastmoney",
                ],
                log_file,
            )
            run_logged([sys.executable, "scripts/pipeline/ocr_discover_queue.py"], log_file)
            result["reports"] = {"imported": True}

        market_plan = plan.get("market") or {}
        market_needs_retry = bool(
            market_plan.get("needs_retry")
            or market_plan.get("stale_or_failed_state_rows", 0) > 0
        )
        if args.skip_market:
            result["market"] = {"skipped": "skip-market"}
        elif not args.include_market and market_needs_retry:
            result["market"] = {"skipped": "market retry requires --include-market"}
        elif market_needs_retry:
            target_date = str(market_plan.get("target_date") or plan.get("plan_date"))
            run_logged(
                [
                    sys.executable, "scripts/pipeline/market_kline_sina_backfill.py",
                    "--start", target_date, "--end", target_date,
                    "--asset-type", "stock", "--workers", "4",
                    "--success-state-job", "daily_close",
                ],
                log_file,
            )
            result["market"] = {"sina_daily_backfill": True, "target_date": target_date}

        update_plan_status(plan_id, "ok", result)
        print(f"[retry_plan] complete id={plan_id}")
        return 0
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        update_plan_status(plan_id, "failed", result)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
