#!/usr/bin/env python3
from __future__ import annotations

import sys

import argparse
import datetime as dt

from common import crawler_run, iso, log_path, run_logged, today


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh daily analytics layers after report/news/market ingestion.")
    parser.add_argument("--date", help="Analysis date YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--skip-schema", action="store_true")
    parser.add_argument("--skip-kg", action="store_true")
    parser.add_argument("--skip-technical", action="store_true")
    parser.add_argument("--event-lookback-days", type=int, default=3)
    parser.add_argument("--research-lookback-days", type=int, default=14)
    parser.add_argument("--forecast-lookback-days", type=int, default=180)
    parser.add_argument("--kg-lookback-days", type=int, default=30)
    parser.add_argument("--skip-search-sync", action="store_true")
    args = parser.parse_args()

    as_of = dt.date.fromisoformat(args.date) if args.date else today()
    event_since = as_of - dt.timedelta(days=args.event_lookback_days)
    research_since = as_of - dt.timedelta(days=args.research_lookback_days)
    kg_since = as_of - dt.timedelta(days=args.kg_lookback_days)
    label_since = as_of - dt.timedelta(days=60)

    log_file = log_path("analytics_daily")
    with crawler_run("daily_analytics", "analytics", research_since, as_of, log_file):
        if not args.skip_schema:
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_analytics_signals.sql",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_finance_fact_checks.sql",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_agent_knowledge.sql",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_ai_hardware_graph.sql",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_market_knowledge_graph.sql",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_point_in_time.sql",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_market_knowledge_graph_v3.sql",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_factor_research.sql",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_temporal_knowledge_graph.sql",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/apply_schema.py",
                    "--admin",
                    "--sql",
                    "docker/postgres/schema_agent_portfolio.sql",
                ],
                log_file,
            )

        run_logged(
            [
                sys.executable,
                "scripts/analytics/refresh_point_in_time_metadata.py",
                "--as-of",
                iso(as_of),
                "--skip-financial-backfill",
            ],
            log_file,
        )
        run_logged(
            [
                sys.executable,
                "scripts/analytics/build_asset_forecast_daily.py",
                "--as-of",
                iso(as_of),
                "--lookback-days",
                str(args.forecast_lookback_days),
            ],
            log_file,
        )
        run_logged(
            [
                sys.executable,
                "scripts/analytics/build_research_signals.py",
                "--since",
                iso(research_since),
                "--until",
                iso(as_of),
            ],
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
        if not args.skip_technical:
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/build_technical_signals.py",
                    "--since",
                    iso(research_since),
                    "--until",
                    iso(as_of),
                ],
                log_file,
            )
        run_logged(
            [
                sys.executable,
                "scripts/analytics/build_fusion_signals.py",
                "--since",
                iso(research_since),
                "--until",
                iso(as_of),
            ],
            log_file,
        )
        run_logged(
            [
                sys.executable,
                "scripts/analytics/build_asset_snapshot_daily.py",
                "--as-of",
                iso(as_of),
            ],
            log_file,
        )

        if not args.skip_kg:
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
                    sys.executable,
                    "scripts/analytics/build_temporal_graph_features_daily.py",
                    "--as-of",
                    iso(as_of),
                ],
                log_file,
            )

        if not args.skip_technical:
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/build_factor_features.py",
                    "--since",
                    iso(research_since),
                    "--until",
                    iso(as_of),
                    "--chunk-days",
                    "31",
                    "--skip-labels",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/build_factor_features.py",
                    "--since",
                    iso(label_since),
                    "--until",
                    iso(as_of),
                    "--chunk-days",
                    "31",
                    "--labels-only",
                ],
                log_file,
            )
            run_logged(
                [
                    sys.executable,
                    "scripts/analytics/build_factor_scores.py",
                    "--since",
                    iso(research_since),
                    "--until",
                    iso(as_of),
                ],
                log_file,
            )

        if not args.skip_search_sync:
            run_logged(
                [
                    sys.executable,
                    "scripts/search/sync_document_search.py",
                    "--skip-schema",
                    "--queue",
                    "--skip-meili",
                    "--queue-limit",
                    "20000",
                    "--queue-batch-size",
                    "50",
                    "--body-chars",
                    "6000",
                ],
                log_file,
            )

    print(f"[daily_analytics] complete as_of={iso(as_of)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
