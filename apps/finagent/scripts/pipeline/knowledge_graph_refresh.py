#!/usr/bin/env python3
"""Refresh document-derived market and temporal knowledge graphs."""
from __future__ import annotations

import sys

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import datetime as dt
import subprocess
import os

from common import (
    TZ, ROOT, load_env,
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

GRAPH_SCHEMAS = (
    "docker/postgres/schema_ai_hardware_graph.sql",
    "docker/postgres/schema_market_knowledge_graph.sql",
    "docker/postgres/schema_market_knowledge_graph_v3.sql",
    "docker/postgres/schema_temporal_knowledge_graph.sql",
)


def core_ready(as_of: dt.date) -> bool:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT status
            FROM ops.crawler_runs
            WHERE task_name = 'daily_analytics_core'
              AND end_date = %s
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (as_of,),
        ).fetchone()
    return bool(row and row["status"] == "ok")


def latest_core_date(as_of: dt.date) -> dt.date | None:
    """Use the latest completed core date when a persistent timer runs after midnight."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT max(end_date) AS end_date
            FROM ops.crawler_runs
            WHERE task_name = 'daily_analytics_core'
              AND status = 'ok'
              AND end_date <= %s
            """,
            (as_of,),
        ).fetchone()
    return row["end_date"] if row and row["end_date"] else None


def graph_feature_start(as_of: dt.date, lookback_days: int) -> dt.date:
    start = as_of - dt.timedelta(days=lookback_days)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT max(dt) AS max_dt
            FROM analytics.graph_feature_daily
            WHERE dt <= %s
            """,
            (as_of,),
        ).fetchone()
    if row and row["max_dt"]:
        start = max(start, row["max_dt"] + dt.timedelta(days=1))
    return min(start, as_of)


def dirty_start(
    as_of: dt.date,
    high_watermark: dt.datetime | None,
    cutoff: dt.datetime,
    default_days: int,
    max_replay_days: int,
) -> dt.date:
    if high_watermark is None:
        return as_of - dt.timedelta(days=default_days)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT min(effective_date) AS dirty_from
            FROM (
              SELECT COALESCE(published_at::date, created_at::date) AS effective_date
              FROM corpus.documents
              WHERE updated_at > %s AND updated_at <= %s
              UNION ALL
              SELECT report_date
              FROM research.reports
              WHERE updated_at > %s AND updated_at <= %s
            ) changed
            WHERE effective_date IS NOT NULL AND effective_date <= %s
            """,
            (high_watermark, cutoff, high_watermark, cutoff, as_of),
        ).fetchone()
    changed = row["dirty_from"] if row else None
    if changed is None:
        return as_of
    return max(changed, as_of - dt.timedelta(days=max_replay_days))


def main() -> int:
    for key,value in load_env(ROOT / '.env.finance-agent-v3').items():
        os.environ.setdefault(key,value)
    parser = argparse.ArgumentParser(description="Refresh market and temporal knowledge graphs after analytics core.")
    parser.add_argument("--date", help="Analysis date YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--mode", choices=("daily", "repair"), default="daily")
    parser.add_argument("--repair-since", help="Explicit historical start; requires --mode repair")
    parser.add_argument("--apply-schema", action="store_true", help="Apply graph schemas before refresh; reserved for maintenance runs.")
    parser.add_argument("--ignore-core-gate", action="store_true")
    parser.add_argument("--event-lookback-days", type=int, default=3)
    parser.add_argument("--kg-lookback-days", type=int, default=30)
    parser.add_argument(
        "--max-replay-days",
        type=int,
        default=45,
        help="Maximum daily replay window. Use a larger value for explicit historical backfills.",
    )
    parser.add_argument(
        "--feature-lookback-days",
        type=int,
        default=14,
        help="Daily graph-feature rebuild window; explicit historical runs use the full replay window.",
    )
    parser.add_argument("--llm-limit", type=int, default=30)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--llm-required", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    requested_as_of = dt.date.fromisoformat(args.date) if args.date else today()
    with connect() as conn:
        as_of, market_status = complete_market_date(conn, requested_as_of)
    print(f"[data_date_gate] {market_status}", flush=True)
    if not args.date and not args.ignore_core_gate and not core_ready(as_of):
        completed = latest_core_date(as_of)
        if completed:
            as_of = completed
            print(
                f"[knowledge_graph] upstream core for {iso(requested_as_of)} is not ready; "
                f"running latest completed date {iso(as_of)}"
            )
    cutoff = dt.datetime.now(TZ)
    watermark = get_pipeline_watermark("knowledge_graph")
    high_watermark = watermark.get("source_high_watermark") if watermark else None
    candidate_since = dirty_start(
        as_of, high_watermark, cutoff, args.kg_lookback_days, 36500,
    )
    kg_since = choose_window(candidate_since, as_of, args.mode, daily_days=3,
        repair_since=dt.date.fromisoformat(args.repair_since) if args.repair_since else None)
    event_since = min(kg_since, as_of - dt.timedelta(days=args.event_lookback_days))
    feature_since = kg_since
    log_file = log_path("knowledge_graph")
    if args.dry_run:
        print(
            f"[knowledge_graph] dry-run as_of={iso(as_of)} "
            f"event_since={iso(event_since)} kg_since={iso(kg_since)}"
        )
        return 0

    with pipeline_lock("knowledge_graph"):
        try:
            with connect() as conn:
                retain_older(conn, "knowledge_graph", candidate_since, kg_since, cutoff)
            with crawler_run("daily_knowledge_graph", "analytics", kg_since, as_of, log_file) as state:
                set_pipeline_watermark(
                    "knowledge_graph", "running", run_id=state["id"],
                    dirty_from_date=kg_since, dirty_to_date=as_of,
                )
                if not args.ignore_core_gate and not core_ready(as_of):
                    raise RuntimeError(f"daily_analytics_core has not succeeded for {iso(as_of)}")

                if args.apply_schema:
                    for sql_path in GRAPH_SCHEMAS:
                        run_logged(
                            [sys.executable, "scripts/analytics/apply_schema.py", "--admin", "--sql", sql_path],
                            log_file,
                        )

                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/extract_research_target_price.py",
                        "--since",
                        iso(kg_since),
                        "--limit",
                        "5000",
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/extract_document_events_rules.py",
                        "--drain",
                        "--include-updated-announcements-hours",
                        "0" if args.mode == "daily" else "48",
                        "--since",
                        iso(event_since),
                        "--until",
                        iso(as_of),
                        "--limit",
                        "10000",
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/queue_temporal_llm_candidates.py",
                        "--since",
                        iso(kg_since),
                        "--until",
                        iso(as_of),
                        "--limit",
                        "20000",
                    ],
                    log_file,
                )
                if not args.skip_llm and args.llm_limit > 0:
                    try:
                        run_logged(
                            [
                                sys.executable,
                                "scripts/analytics/extract_temporal_candidates_llm.py",
                                "--limit",
                                str(args.llm_limit),
                            ],
                            log_file,
                        )
                    except subprocess.CalledProcessError:
                        if args.llm_required:
                            raise
                        print("[knowledge_graph] bounded LLM extraction failed; queued candidates remain retryable")

                run_logged([sys.executable, "scripts/analytics/seed_ai_hardware_graph.py"], log_file)
                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/discover_ai_hardware_concepts.py",
                        "--since",
                        iso(kg_since),
                        "--until",
                        iso(as_of),
                        "--limit",
                        "20000",
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/classify_ai_hardware_candidates.py",
                        "--rules-only",
                        "--limit",
                        "10000",
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/promote_ai_hardware_candidates.py",
                        "--min-confidence",
                        "0.75",
                        "--limit",
                        "10000",
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/build_ai_hardware_theme_scores.py",
                        "--as-of",
                        iso(as_of),
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/build_market_knowledge_graph.py",
                        "--as-of",
                        iso(as_of),
                        "--lookback-days",
                        str(args.kg_lookback_days),
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/build_relation_claims.py",
                        "--as-of",
                        iso(as_of),
                        "--lookback-days",
                        str(args.kg_lookback_days),
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable,
                        "scripts/analytics/build_temporal_knowledge_graph.py",
                        "--since",
                        iso(kg_since),
                        "--until",
                        iso(as_of),
                    ],
                    log_file,
                )
                run_logged(
                    [
                        sys.executable, "scripts/analytics/build_temporal_graph_features_daily.py",
                        "--since", iso(feature_since), "--until", iso(as_of),
                        "--knowledge-mode", "public" if args.mode == "repair" else "system",
                    ],
                    log_file,
                )
            with connect() as conn:
                finish_covered(conn, "knowledge_graph", kg_since, as_of, cutoff)
            set_pipeline_watermark(
                "knowledge_graph", "ok", run_id=state["id"],
                source_high_watermark=cutoff, dirty_from_date=kg_since, dirty_to_date=as_of,
            )
        except Exception:
            set_pipeline_watermark(
                "knowledge_graph", "failed", dirty_from_date=kg_since, dirty_to_date=as_of,
            )
            raise

    print(f"[knowledge_graph] complete as_of={iso(as_of)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
