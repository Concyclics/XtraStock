#!/usr/bin/env python3
"""Materialize an unadjusted, quality-gated canonical daily series."""
from __future__ import annotations

import argparse
import datetime as dt
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from common import connect

import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from v3_ops import record_quality, set_dataset_watermark, stage_run  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")

SELECT_SQL = """
CREATE TEMP TABLE canonical_daily_v2_selected ON COMMIT DROP AS
WITH candidates AS (
  SELECT
    coalesce(ia.canonical_asset_id,b.asset_id) asset_id,
    b.asset_id AS original_asset_id,ia.corrected_at AS identity_corrected_at,
    (b.ts AT TIME ZONE 'Asia/Shanghai')::date AS dt,
    b.open,b.high,b.low,b.close,
    CASE WHEN b.source='sina:kline:day:none' THEN b.volume/100.0 ELSE b.volume END AS volume,
    b.amount,b.source,b.ts AS source_ts,
    greatest(coalesce(b.updated_at,b.created_at,now()),ia.corrected_at) AS source_updated_at,
    CASE b.source
      WHEN 'eastmoney:kline:day:none' THEN 1
      WHEN 'sina:kline:day:none' THEN 2
      ELSE 99
    END AS source_priority
  FROM market.bars_daily b
  LEFT JOIN market.asset_identity_aliases ia ON ia.alias_asset_id=b.asset_id
  JOIN market.assets a ON a.asset_id=coalesce(ia.canonical_asset_id,b.asset_id)
  WHERE a.asset_type=ANY(%(asset_types)s)
    AND NOT (a.metadata ? 'identity_alias_of')
    AND (%(asset_ids)s::uuid[] IS NULL OR a.asset_id=ANY(%(asset_ids)s::uuid[]))
    AND b.source IN ('eastmoney:kline:day:none','sina:kline:day:none')
    AND b.ts >= (%(since)s::date::timestamp AT TIME ZONE 'Asia/Shanghai')
    AND b.ts < ((%(until)s::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
    AND b.close IS NOT NULL AND b.close > 0
), source_stats AS (
  SELECT asset_id,dt,min(close) AS min_source_close,max(close) AS max_source_close,
         count(DISTINCT source) AS source_count
  FROM candidates
  GROUP BY asset_id,dt
), compared AS (
  SELECT c.*,s.min_source_close,s.max_source_close,s.source_count,
         row_number() OVER (
           PARTITION BY c.asset_id,c.dt
           ORDER BY c.source_priority,c.source_updated_at DESC
         ) AS rn
  FROM candidates c
  JOIN source_stats s USING(asset_id,dt)
), selected AS (
  SELECT *,
         CASE
           WHEN open IS NULL OR high IS NULL OR low IS NULL
             OR high < greatest(open,close) OR low > least(open,close) OR low < 0
             THEN 'rejected'
           WHEN abs(max_source_close-min_source_close)/nullif(min_source_close,0) > %(max_close_diff)s
             THEN 'conflict'
           WHEN source_count > 1 THEN 'verified'
           ELSE 'unverified'
         END AS quality_status
  FROM compared
  WHERE rn=1
)
SELECT
  asset_id,dt,open,high,low,close,volume,amount,source,'none'::text AS adjustment,
  'raw_unadjusted'::text AS price_basis,source_ts,source_updated_at,
  source <> 'eastmoney:kline:day:none' AS is_fallback,
  greatest(
    source_updated_at,
    (dt::timestamp + time '15:05') AT TIME ZONE 'Asia/Shanghai'
  ) AS available_at,
  quality_status,
  jsonb_strip_nulls(jsonb_build_object(
    'volume_unit','lot','source_priority',source_priority,'source_count',source_count,
    'original_asset_id',original_asset_id,'identity_corrected_at',identity_corrected_at,
    'min_source_close',min_source_close,'max_source_close',max_source_close,
    'relative_source_close_diff',
      CASE WHEN source_count > 1 THEN abs(max_source_close-min_source_close)/nullif(min_source_close,0) END,
    'fallback_reason',CASE WHEN source_priority > 1 THEN 'eastmoney_unadjusted_unavailable' END
  )) AS quality
FROM selected
"""


# Current-system-known lifecycle reconstruction; never an assertion that this
# state was known historically. Historical features retain separate PIT cutoffs.
try:
    from .lifecycle_evidence import lifecycle_order
except ImportError:
    from lifecycle_evidence import lifecycle_order

COVERAGE_SQL = f"""
WITH stock_universe AS (
  SELECT asset_id FROM market.assets WHERE asset_type='stock'
    AND NOT (metadata ? 'identity_alias_of')
), lifecycle_latest AS MATERIALIZED (
  SELECT DISTINCT ON (v.asset_id) v.*
  FROM market.asset_lifecycle_versions v JOIN stock_universe a USING(asset_id)
  WHERE v.valid_from <= %(expected_date)s
    AND (v.valid_to IS NULL OR v.valid_to >= %(expected_date)s)
    AND v.available_at <= %(lifecycle_cutoff)s
  ORDER BY v.asset_id,{lifecycle_order('v')}
), eligible_members AS (
  SELECT asset_id FROM lifecycle_latest
  WHERE lifecycle_status='active' AND is_tradable AND NOT is_suspended AND quality_status<>'conflict'
), eligible AS (SELECT count(*) AS assets FROM eligible_members),
unknown_members AS (
  SELECT a.asset_id FROM stock_universe a LEFT JOIN lifecycle_latest v USING(asset_id)
  WHERE v.asset_id IS NULL OR v.lifecycle_status='unknown' OR v.quality_status='conflict'
), unknown_lifecycle_candidates AS (SELECT count(*) AS assets FROM unknown_members),
unknown_in_actual AS (
  SELECT count(DISTINCT b.asset_id) AS assets FROM market.canonical_bars_daily_v2 b
  JOIN unknown_members u USING(asset_id) WHERE b.dt=%(expected_date)s
), nontradable_in_actual AS (
  SELECT count(DISTINCT b.asset_id) AS assets FROM market.canonical_bars_daily_v2 b
  JOIN lifecycle_latest v USING(asset_id) WHERE b.dt=%(expected_date)s
    AND v.lifecycle_status<>'unknown' AND v.quality_status<>'conflict'
    AND (v.lifecycle_status<>'active' OR NOT v.is_tradable OR v.is_suspended)
), actual AS (
  SELECT count(DISTINCT b.asset_id) FILTER (WHERE b.asset_id IN (SELECT asset_id FROM eligible_members)) AS assets,
    count(DISTINCT b.asset_id) AS observed_assets,
    count(*) FILTER (WHERE b.quality_status='conflict') AS conflicts,
    count(*) FILTER (WHERE b.quality_status='rejected') AS rejected,
    count(*) FILTER (WHERE b.is_fallback) AS fallback,count(*) AS rows
  FROM market.canonical_bars_daily_v2 b JOIN stock_universe a USING(asset_id)
  WHERE b.dt=%(expected_date)s
)
SELECT eligible.assets AS eligible_assets,actual.assets AS actual_assets,actual.observed_assets,
  unknown_lifecycle_candidates.assets AS unknown_lifecycle_assets,
  unknown_in_actual.assets AS unknown_actual_assets,
  nontradable_in_actual.assets AS nontradable_actual_assets,
  actual.conflicts,actual.rejected,actual.fallback,actual.rows,
  CASE WHEN eligible.assets=0 THEN 0 ELSE 100.0*actual.assets/eligible.assets END AS coverage_pct
FROM eligible CROSS JOIN unknown_lifecycle_candidates CROSS JOIN unknown_in_actual
CROSS JOIN nontradable_in_actual CROSS JOIN actual
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build dual-track canonical unadjusted daily bars V2.")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--asset-types", default="stock,index")
    parser.add_argument("--asset-ids-json", type=Path, help="Optional JSON list for an identity-repair partition; does not redefine coverage denominator")
    parser.add_argument("--min-coverage-pct", type=float, default=99.0)
    parser.add_argument("--max-close-diff", type=float, default=0.02)
    parser.add_argument("--allow-quality-fail", action="store_true")
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since must not be after --until")
    asset_types = [value.strip() for value in args.asset_types.split(",") if value.strip()]

    asset_ids = __import__("json").loads(args.asset_ids_json.read_text()) if args.asset_ids_json else None
    if asset_ids == []: parser.error("asset ID filter cannot be empty")
    expected_date = until
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            SELECT max(trade_date) AS expected_date
            FROM market.trading_calendar
            WHERE exchange='SSE' AND session_status='open'
              AND trade_date BETWEEN %s AND %s
            """,
            (since, until),
        ).fetchone()
        if row and row["expected_date"]:
            expected_date = row["expected_date"]

    with stage_run(
        "finance_v3_evening", "canonical_daily_v2",
        expected_date=expected_date,
        metadata={
            "since": since.isoformat(), "until": until.isoformat(),
            "asset_types": asset_types, "min_coverage_pct": args.min_coverage_pct,
            "max_close_diff": args.max_close_diff,
            "asset_ids": asset_ids,
        },
    ) as run:
        params = {
            "since": since,
            "until": until,
            "asset_types": asset_types,
            "max_close_diff": args.max_close_diff,
            "asset_ids": asset_ids,
        }
        lifecycle_cutoff = dt.datetime.now(dt.timezone.utc)
        with connect(autocommit=False, admin=False) as conn:
            with conn.cursor() as cur:
                cur.execute(SELECT_SQL, params)
                cur.execute(
                    """
                    DELETE FROM market.canonical_bars_daily_v2
                    WHERE dt BETWEEN %(since)s AND %(until)s
                      AND asset_id IN (
                        SELECT asset_id FROM market.assets
                        WHERE asset_type=ANY(%(asset_types)s)
                          AND NOT (metadata ? 'identity_alias_of')
                          AND (%(asset_ids)s::uuid[] IS NULL OR asset_id=ANY(%(asset_ids)s::uuid[]))
                      )
                    """,
                    params,
                )
                cur.execute(
                    """
                    INSERT INTO market.canonical_bars_daily_v2 (
                      asset_id,dt,open,high,low,close,volume,amount,source,adjustment,
                      price_basis,source_ts,source_updated_at,is_fallback,available_at,
                      quality_status,quality,source_version,updated_at
                    )
                    SELECT asset_id,dt,open,high,low,close,volume,amount,source,adjustment,
                           price_basis,source_ts,source_updated_at,is_fallback,available_at,
                           quality_status,quality,'canonical_daily:v2',now()
                    FROM canonical_daily_v2_selected
                    """
                )
                cur.execute(
                    COVERAGE_SQL,
                    {**params, "expected_date": expected_date, "lifecycle_cutoff": lifecycle_cutoff},
                )
                latest = cur.fetchone()
                cur.execute(
                    """
                    SELECT count(*) AS rows,min(dt) AS min_dt,max(dt) AS max_dt,
                           count(*) FILTER (WHERE quality_status='conflict') AS conflicts,
                           count(*) FILTER (WHERE quality_status='rejected') AS rejected,
                           count(*) FILTER (WHERE is_fallback) AS fallback,
                           count(DISTINCT asset_id) AS assets
                    FROM canonical_daily_v2_selected
                    """
                )
                summary = cur.fetchone()
            conn.commit()

        eligible = int(latest["eligible_assets"] or 0)
        actual = int(latest["actual_assets"] or 0)
        coverage = float(latest["coverage_pct"] or 0)
        conflicts = int(summary["conflicts"] or 0)
        rejected = int(summary["rejected"] or 0)
        expected_rows = int(latest["rows"] or 0)
        unknown_lifecycle = int(latest["unknown_lifecycle_assets"] or 0)
        unknown_actual = int(latest["unknown_actual_assets"] or 0)
        nontradable_actual = int(latest["nontradable_actual_assets"] or 0)
        calendar_known = expected_date is not None
        quality_fail = (
            not calendar_known
            or coverage < args.min_coverage_pct
            or int(latest["conflicts"] or 0) > 0
            or int(latest["rejected"] or 0) > 0
            or unknown_actual > 0
            or nontradable_actual > 0
        )
        latest_fallback = int(latest["fallback"] or 0)
        quality = "fail" if quality_fail else (
            "warn" if latest_fallback > 0 or unknown_lifecycle > 0 else "pass"
        )

        record_quality(
            run["run_id"], "market.canonical_bars_daily_v2", "latest_eligible_coverage",
            status="pass" if coverage >= args.min_coverage_pct else "fail", severity="error",
            observed_value=coverage, threshold_value=args.min_coverage_pct,
            affected_rows=max(eligible-actual, 0),
            details={"expected_date": expected_date.isoformat(), "eligible": eligible, "actual": actual},
        )
        record_quality(
            run["run_id"], "market.canonical_bars_daily_v2", "unknown_pit_lifecycle",
            status="pass" if unknown_actual == 0 else "fail", severity="error",
            observed_value=float(unknown_actual), threshold_value=0,
            affected_rows=unknown_actual,
            details={
                "reason": "unknown lifecycle cannot be relabeled as suspension from missing bars",
                "unknown_candidates_excluded_from_universe": unknown_lifecycle,
                "scope": "hard failure only when an unknown asset enters canonical rows",
            },
        )
        record_quality(
            run["run_id"], "market.canonical_bars_daily_v2", "nontradable_actual_prices",
            status="fail" if nontradable_actual else "pass", severity="error",
            observed_value=float(nontradable_actual), threshold_value=0,
            affected_rows=nontradable_actual,
            details={"resolution":"latest system-known lifecycle valid on target date",
                     "lifecycle_cutoff":lifecycle_cutoff.isoformat()},
        )
        record_quality(
            run["run_id"], "market.canonical_bars_daily_v2", "cross_source_conflicts",
            status="pass" if conflicts == 0 else "fail", severity="error",
            observed_value=float(conflicts), threshold_value=0, affected_rows=conflicts,
        )
        record_quality(
            run["run_id"], "market.canonical_bars_daily_v2", "invalid_ohlc",
            status="pass" if rejected == 0 else "fail", severity="error",
            observed_value=float(rejected), threshold_value=0, affected_rows=rejected,
        )
        record_quality(
            run["run_id"], "market.canonical_bars_daily_v2", "primary_source_fallback",
            status="warn" if latest_fallback else "pass", severity="warning",
            observed_value=float(latest_fallback), threshold_value=0,
            affected_rows=latest_fallback,
            details={
                "expected_primary": "eastmoney:kline:day:none",
                "fallback_source": "sina:kline:day:none",
                "source_switch_requires_monitoring": True,
            },
        )
        data_through = dt.datetime.combine(expected_date, dt.time(15, 5), TZ)
        set_dataset_watermark(
            "market.canonical_bars_daily_v2", schema_version="v2", run_id=run["run_id"],
            expected_date=expected_date, data_through=data_through,
            row_count=expected_rows, eligible_count=eligible, coverage_pct=coverage,
            quality_status=quality, source_version="canonical_daily:v2",
            details={
                "range": [str(summary["min_dt"]), str(summary["max_dt"])],
                "all_rows": int(summary["rows"] or 0), "all_assets": int(summary["assets"] or 0),
                "conflicts": conflicts, "rejected": rejected,
                "fallback": int(summary["fallback"] or 0),
                "eligible_semantics": "latest system-known tradable lifecycle version valid on target date",
                "lifecycle_cutoff":lifecycle_cutoff.isoformat(),
                "lifecycle_resolution":lifecycle_order(),
                "nontradable_actual_assets":nontradable_actual,
                "unknown_lifecycle_assets": unknown_lifecycle,
                "unknown_actual_assets": unknown_actual,"observed_stock_assets":int(latest["observed_assets"] or 0),"coverage_numerator":"observed intersect eligible",
            },
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {
            "market.canonical_bars_daily_v2": expected_date.isoformat(),
            "coverage_pct": coverage,
        }
        if quality_fail and not args.allow_quality_fail:
            raise RuntimeError(
                f"canonical V2 hard gate failed date={expected_date} coverage={coverage:.2f}% "
                f"conflicts={int(latest['conflicts'] or 0)} rejected={int(latest['rejected'] or 0)} "
                f"unknown_actual={unknown_actual} nontradable_actual={nontradable_actual}"
            )

    print({
        "range": [str(summary["min_dt"]), str(summary["max_dt"])],
        "rows": int(summary["rows"] or 0), "assets": int(summary["assets"] or 0),
        "expected_date": expected_date.isoformat(), "coverage_pct": round(coverage, 3),
        "conflicts": conflicts, "rejected": rejected,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
