#!/usr/bin/env python3
"""Materialize daily factor features and scores after graph refresh."""
from __future__ import annotations

import sys

import argparse
import datetime as dt

from common import (
    TZ,
    connect,
    crawler_run,
    get_pipeline_watermark,
    iso,
    log_path,
    pipeline_lock,
    run_logged,
    set_pipeline_watermark,
    today,
)

from market_data_date import complete_market_date
from repair_windows import choose_window, retain_older, finish_covered

def graph_ready(as_of: dt.date) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT status
            FROM ops.crawler_runs
            WHERE task_name = 'daily_knowledge_graph' AND end_date = %s
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (as_of,),
        ).fetchone()
    return bool(row and row["status"] == "ok")


def latest_graph_date(as_of: dt.date) -> dt.date | None:
    """Use the latest completed graph date for persistent post-reboot retries."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT max(end_date) AS end_date
            FROM ops.crawler_runs
            WHERE task_name = 'daily_knowledge_graph'
              AND status = 'ok'
              AND end_date <= %s
            """,
            (as_of,),
        ).fetchone()
    return row["end_date"] if row and row["end_date"] else None


def dirty_start(
    as_of: dt.date,
    high_watermark: dt.datetime | None,
    cutoff: dt.datetime,
    default_days: int,
    max_replay_days: int,
) -> dt.date:
    if high_watermark is None:
        changed = as_of - dt.timedelta(days=default_days)
    else:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT min(effective_date) AS dirty_from
                FROM (
                  SELECT dt AS effective_date FROM analytics.asset_technical_daily
                  WHERE updated_at > %s AND updated_at <= %s
                  UNION ALL
                  SELECT dt FROM analytics.asset_research_daily
                  WHERE updated_at > %s AND updated_at <= %s
                  UNION ALL
                  SELECT event_date FROM analytics.document_events
                  WHERE updated_at > %s AND updated_at <= %s
                  UNION ALL
                  SELECT dt FROM analytics.graph_feature_daily
                  WHERE updated_at > %s AND updated_at <= %s
                  UNION ALL
                  SELECT dt FROM market.canonical_bars_daily_v2
                  WHERE updated_at > %s AND updated_at <= %s
                  UNION ALL
                  SELECT ts::date FROM market.money_flow_daily
                  WHERE created_at > %s AND created_at <= %s
                ) changed_rows
                WHERE effective_date IS NOT NULL AND effective_date <= %s
                """,
                (
                    high_watermark, cutoff, high_watermark, cutoff,
                    high_watermark, cutoff, high_watermark, cutoff,
                    high_watermark, cutoff, high_watermark, cutoff, as_of,
                ),
            ).fetchone()
        changed = row["dirty_from"] if row and row["dirty_from"] else as_of

    graph_watermark = get_pipeline_watermark("knowledge_graph")
    graph_dirty = graph_watermark.get("dirty_from_date") if graph_watermark else None
    if graph_dirty:
        changed = min(changed, graph_dirty)
    return max(changed, as_of - dt.timedelta(days=max_replay_days))


def daily_feature_start(as_of: dt.date, candidate: dt.date, lookback_days: int) -> dt.date:
    """Avoid replaying old correction watermarks during the daily catch-up run."""
    start = max(candidate, as_of - dt.timedelta(days=lookback_days))
    with connect() as conn:
        row = conn.execute(
            """
            SELECT max(dt) AS max_dt
            FROM factor.feature_daily
            WHERE dt <= %s
            """,
            (as_of,),
        ).fetchone()
    if row and row["max_dt"]:
        start = max(start, row["max_dt"] + dt.timedelta(days=1))
    return min(start, as_of)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh factor features and scores after graph materialization.")
    parser.add_argument("--date", help="Analysis date YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--mode", choices=("daily", "repair"), default="daily")
    parser.add_argument("--repair-since", help="Explicit historical start; requires --mode repair")
    parser.add_argument("--ignore-graph-gate", action="store_true")
    parser.add_argument("--default-lookback-days", type=int, default=30)
    parser.add_argument(
        "--max-replay-days",
        type=int,
        default=45,
        help="Maximum daily replay window. Use a larger value for explicit historical backfills.",
    )
    parser.add_argument("--feature-lookback-days", type=int, default=14)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    requested_as_of = dt.date.fromisoformat(args.date) if args.date else today()
    with connect() as conn:
        as_of, market_status = complete_market_date(conn, requested_as_of)
    print(f"[data_date_gate] {market_status}", flush=True)
    if not args.date and not args.ignore_graph_gate and not graph_ready(as_of):
        completed = latest_graph_date(as_of)
        if completed:
            as_of = completed
            print(
                f"[factor_daily] upstream graph for {iso(requested_as_of)} is not ready; "
                f"running latest completed date {iso(as_of)}"
            )
    cutoff = dt.datetime.now(TZ)
    watermark = get_pipeline_watermark("factor_daily")
    high_watermark = watermark.get("source_high_watermark") if watermark else None
    candidate_since = dirty_start(
        as_of, high_watermark, cutoff, args.default_lookback_days, 36500,
    )
    since = choose_window(candidate_since, as_of, args.mode, daily_days=3,
        repair_since=dt.date.fromisoformat(args.repair_since) if args.repair_since else None)
    log_file = log_path("factor_daily")
    if args.dry_run:
        print(f"[factor_daily] dry-run as_of={iso(as_of)} since={iso(since)}")
        return 0

    with pipeline_lock("factor_daily"):
        try:
            with connect() as conn:
                retain_older(conn, "factor_daily", candidate_since, since, cutoff)
            with crawler_run("daily_factor_materialize", "factor", since, as_of, log_file) as state:
                set_pipeline_watermark(
                    "factor_daily", "running", run_id=state["id"],
                    dirty_from_date=since, dirty_to_date=as_of,
                )
                if not args.ignore_graph_gate and not graph_ready(as_of):
                    raise RuntimeError(f"daily_knowledge_graph has not succeeded for {iso(as_of)}")

                run_logged(
                    [
                        sys.executable, "scripts/analytics/build_factor_features.py",
                        "--since", iso(since), "--until", iso(as_of),
                        "--chunk-days", "31", "--skip-labels",
                        "--knowledge-mode", "public" if args.mode == "repair" else "system",
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable, "scripts/analytics/build_factor_scores.py",
                        "--since", iso(since), "--until", iso(as_of),
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable, "scripts/analytics/refresh_factor_health.py",
                        "--since", iso(since), "--until", iso(as_of),
                    ],
                    log_file,
                )
                if args.mode == "daily":
                    run_logged([sys.executable, "scripts/analytics/build_financial_factor_features.py",
                        "--as-of", iso(today())],log_file)
            with connect() as conn:
                finish_covered(conn, "factor_daily", since, as_of, cutoff)
            set_pipeline_watermark(
                "factor_daily", "ok", run_id=state["id"],
                source_high_watermark=cutoff, dirty_from_date=since, dirty_to_date=as_of,
            )
        except Exception:
            set_pipeline_watermark(
                "factor_daily", "failed", dirty_from_date=since, dirty_to_date=as_of,
            )
            raise

    print(f"[factor_daily] complete since={iso(since)} as_of={iso(as_of)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
