#!/usr/bin/env python3
from __future__ import annotations

import argparse

from common import connect


TABLES = (
    "analytics.asset_technical_daily",
    "analytics.factor_score_latest",
    "analytics.graph_adjacency_latest",
    "corpus.document_search_index",
    "corpus.document_quality",
    "company.filings",
    "company.financial_facts",
    "market.money_flow_daily",
    "market.trading_calendar",
    "market.asset_lifecycle_versions",
    "market.corporate_actions",
    "market.adjustment_factors_daily",
    "market.canonical_bars_daily_v2",
    "factor.daily_scores",
    "kg.relation_versions",
    "ops.ocr_queue",
    "market.crawl_state",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run targeted non-blocking PostgreSQL maintenance.")
    parser.add_argument("--table", action="append", choices=TABLES, help="Table to vacuum; repeatable. Defaults to all targets.")
    parser.add_argument("--analyze-only", action="store_true")
    args = parser.parse_args()
    targets = tuple(args.table or TABLES)

    with connect(admin=True) as conn:
        conn.autocommit = True
        for table in targets:
            statement = f"ANALYZE {table}" if args.analyze_only else f"VACUUM (ANALYZE) {table}"
            conn.execute(statement)
            print(f"[database_maintenance] complete {statement}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
