#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
from typing import Any

from common import TZ, connect, today
from data_freshness_v3 import freshness_issue
from v3_ops import record_quality, set_dataset_watermark, stage_run


REQUIRED_DATASETS = (
    "market.trading_calendar",
    "market.canonical_bars_daily_v2",
    "market.asset_lifecycle_versions",
    "market.corporate_actions",
    "market.adjustment_factors_daily",
    "company.filings",
    "company.financial_facts",
    "corpus.document_quality",
    "corpus.document_embeddings",
    "factor.daily_scores",
    "kg.temporal_graph",
)


def previous_market_date(run_date: dt.date) -> dt.date | None:
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            SELECT max(trade_date) AS trade_date
            FROM market.trading_calendar
            WHERE exchange='SSE' AND session_status IN ('open','half_day') AND trade_date < %s
            """,
            (run_date,),
        ).fetchone()
    return row["trade_date"] if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the immutable V3 morning data gate.")
    parser.add_argument("--date")
    args = parser.parse_args()
    run_date = dt.date.fromisoformat(args.date) if args.date else today()
    market_date = previous_market_date(run_date)

    with stage_run("finance_v3_gate", "daily_data_gate", expected_date=run_date) as run:
        with connect(admin=False) as conn:
            rows = conn.execute(
                """
                SELECT dataset_key,expected_date,data_through,row_count,eligible_count,
                       coverage_pct,quality_status,schema_version,source_version,completed_at
                FROM ops.dataset_watermarks
                WHERE dataset_key=ANY(%s) AND partition_key='default'
                """,
                (list(REQUIRED_DATASETS),),
            ).fetchall()
        found = {row["dataset_key"]: dict(row) for row in rows}
        blockers: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for key in REQUIRED_DATASETS:
            row = found.get(key)
            if row is None:
                blockers.append({"dataset_key": key, "reason": "missing_watermark"})
                continue
            if row["quality_status"] == "fail":
                blockers.append({"dataset_key": key, "reason": "quality_fail"})
            elif row["quality_status"] in {"warn", "degraded", "unknown"}:
                warnings.append({"dataset_key": key, "reason": f"quality_{row['quality_status']}"})
            issue = freshness_issue(key, row, run_date, market_date)
            if issue:
                blockers.append({"dataset_key": key, **issue})

        quality = "fail" if blockers else ("warn" if warnings else "pass")
        record_quality(
            run["run_id"], "agent.daily_data_gate", "required_dataset_gate",
            status="fail" if blockers else ("warn" if warnings else "pass"),
            severity="error" if blockers else ("warning" if warnings else "info"),
            observed_value=float(len(blockers)), threshold_value=0.0,
            affected_rows=len(blockers), sample=blockers[:20],
            details={
                "warnings": warnings,
                "market_date": market_date.isoformat() if market_date else None,
            },
        )
        now = dt.datetime.now(TZ)
        set_dataset_watermark(
            "agent.daily_data_gate", schema_version="v3", run_id=run["run_id"],
            expected_date=run_date, data_through=now,
            row_count=len(found), eligible_count=len(REQUIRED_DATASETS),
            coverage_pct=100.0 * len(found) / len(REQUIRED_DATASETS),
            quality_status=quality, source_version="finance-agent-v3:gate:v1",
            details={
                "blockers": blockers,
                "warnings": warnings,
                "market_date": market_date.isoformat() if market_date else None,
            },
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {"agent.daily_data_gate": run_date.isoformat()}

    print({
        "date": run_date.isoformat(), "market_date": market_date.isoformat() if market_date else None,
        "quality_status": quality, "blockers": blockers, "warnings": warnings,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
