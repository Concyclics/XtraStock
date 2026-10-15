#!/usr/bin/env python3
"""Materialize source-versioned scores; archive and invalidate obsolete values."""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect
from factor_dsl import compile_expression


def build_score_sql(expression: str) -> str:
    """One atomic statement; cross-sectional inputs are never stock-code filtered."""
    if ' OVER (' in expression:
        expression = ("CASE WHEN count(*) OVER (PARTITION BY f.dt) >= %(min_cohort_size)s "
                      f"THEN ({expression}) END")
    columns = ["raw_value", "percentile", "zscore", "available_at", "source_feature_date",
               "input_contract_version", "quality_status", "source_feature_updated_at",
               "source_factor_version", "source_cohort_hash", "source_decision_cutoff", "invalid_reason"]
    distinct = "ROW(" + ','.join('s.'+x for x in columns) + ") IS DISTINCT FROM ROW(" + ','.join('n.'+x for x in columns) + ')'
    updates = ','.join(f'{x}=EXCLUDED.{x}' for x in columns)
    return f"""
    WITH cohort_versions AS (
      SELECT f.dt,md5(string_agg(f.asset_id::text||':'||extract(epoch FROM f.updated_at)::text,
          ',' ORDER BY f.asset_id)) AS source_cohort_hash
      FROM factor.feature_daily f JOIN market.assets a USING(asset_id)
      WHERE f.dt BETWEEN %(since)s AND %(until)s AND a.asset_type='stock'
        AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
      GROUP BY f.dt
    ), eligible AS MATERIALIZED (
      SELECT f.* FROM factor.feature_daily f
      JOIN market.assets a USING(asset_id)
      WHERE f.dt BETWEEN %(since)s AND %(until)s
        AND a.asset_type='stock' AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
        AND f.input_contract_version='pit-input-v1' AND f.knowledge_mode='system'
        AND f.input_quality->>'strict_pit_ready'='true'
        AND f.available_at<=f.decision_cutoff AND f.decision_cutoff<=statement_timestamp()
        AND f.decision_cutoff<((f.dt+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
    ), raw AS MATERIALIZED (
      SELECT f.asset_id,f.dt,({expression}) * %(direction)s AS raw_value
      FROM eligible f
    ), scored AS (
      SELECT *, CASE WHEN count(*) OVER (PARTITION BY dt)>=%(min_cohort_size)s
                         AND stddev_samp(raw_value) OVER (PARTITION BY dt)>0
                     THEN percent_rank() OVER (PARTITION BY dt ORDER BY raw_value) END AS percentile,
                CASE WHEN count(*) OVER (PARTITION BY dt)>=%(min_cohort_size)s THEN
                  (raw_value-avg(raw_value) OVER (PARTITION BY dt)) /
                  nullif(stddev_samp(raw_value) OVER (PARTITION BY dt),0) END AS zscore
      FROM raw WHERE raw_value IS NOT NULL
        AND raw_value NOT IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
    ), target_keys AS (
      SELECT f.asset_id,f.dt FROM factor.feature_daily f
      JOIN market.assets a USING(asset_id)
      WHERE f.dt BETWEEN %(since)s AND %(until)s AND a.asset_type='stock'
        AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
      UNION
      SELECT s.asset_id,s.dt FROM factor.daily_scores s
      WHERE s.factor_id=%(factor_id)s AND s.dt BETWEEN %(since)s AND %(until)s
    ), desired AS (
      SELECT %(factor_id)s::uuid AS factor_id,k.asset_id,k.dt,
        r.raw_value,r.percentile,r.zscore,
        coalesce(f.available_at,old.available_at) AS available_at,k.dt AS source_feature_date,
        'pit-input-v1'::text AS input_contract_version,
        CASE WHEN r.raw_value IS NOT NULL THEN 'valid' ELSE 'invalid' END::text AS quality_status,
        f.updated_at AS source_feature_updated_at,%(factor_version)s::int AS source_factor_version,
        cv.source_cohort_hash,f.decision_cutoff AS source_decision_cutoff,
        CASE WHEN f.asset_id IS NULL THEN 'missing_feature'
          WHEN a.asset_type IS DISTINCT FROM 'stock' OR coalesce(a.metadata ? 'identity_alias_of',false) THEN 'ineligible_asset'
          WHEN f.input_contract_version IS DISTINCT FROM 'pit-input-v1' THEN 'legacy_feature_contract'
          WHEN f.knowledge_mode IS DISTINCT FROM 'system' OR f.input_quality->>'strict_pit_ready' IS DISTINCT FROM 'true' THEN 'non_strict_feature'
          WHEN NOT coalesce(f.available_at<=f.decision_cutoff AND f.decision_cutoff<=statement_timestamp()
            AND f.decision_cutoff<((f.dt+1)::timestamp AT TIME ZONE 'Asia/Shanghai'),false) THEN 'invalid_availability'
          WHEN r.raw_value IS NULL THEN 'missing_or_nonfinite_expression'
          ELSE NULL END::text AS invalid_reason
      FROM target_keys k
      LEFT JOIN cohort_versions cv USING(dt)
      -- Explicit bounds retain LEFT JOIN semantics while pruning history chunks.
      LEFT JOIN (SELECT * FROM factor.feature_daily
                 WHERE dt BETWEEN %(since)s AND %(until)s) f USING(asset_id,dt)
      LEFT JOIN market.assets a USING(asset_id)
      LEFT JOIN scored r USING(asset_id,dt)
      LEFT JOIN factor.daily_scores old ON old.factor_id=%(factor_id)s AND old.asset_id=k.asset_id AND old.dt=k.dt
        AND old.dt BETWEEN %(since)s AND %(until)s
      WHERE (%(stock_code)s::text IS NULL OR (a.asset_type='stock' AND a.symbol=%(stock_code)s))
    ), changed AS MATERIALIZED (
      SELECT n.* FROM desired n LEFT JOIN (SELECT * FROM factor.daily_scores
          WHERE dt BETWEEN %(since)s AND %(until)s AND factor_id=%(factor_id)s) s USING(factor_id,asset_id,dt)
      WHERE s.factor_id IS NULL OR {distinct}
    ), archived AS (
      INSERT INTO factor.daily_score_revisions(factor_id,asset_id,dt,previous_row,replacement_reason)
      SELECT s.factor_id,s.asset_id,s.dt,to_jsonb(s),coalesce(n.invalid_reason,'source_or_value_changed')
      FROM changed n JOIN (SELECT * FROM factor.daily_scores
          WHERE dt BETWEEN %(since)s AND %(until)s AND factor_id=%(factor_id)s) s USING(factor_id,asset_id,dt)
      RETURNING revision_id
    ), written AS (
      INSERT INTO factor.daily_scores(factor_id,asset_id,dt,{','.join(columns)},updated_at)
      SELECT factor_id,asset_id,dt,{','.join(columns)},now() FROM changed
      ON CONFLICT(factor_id,asset_id,dt) DO UPDATE SET {updates},updated_at=now()
      RETURNING quality_status
    )
    SELECT count(*) AS changed_rows,count(*) FILTER(WHERE quality_status='invalid') AS invalid_rows,
           (SELECT count(*) FROM archived) AS archived_rows FROM written
    """


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--status", default="shadow,production")
    parser.add_argument("--max-decompressed-tuples", type=int, default=1_000_000)
    parser.add_argument("--stock-code", help="Write only this company, while ranking against the full date cohort.")
    parser.add_argument("--min-cohort-size", type=int, default=2, help="Below this size, retain raw values but withhold ranks.")
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since must be on or before --until")
    statuses = [x.strip() for x in args.status.split(",") if x.strip()]
    if args.max_decompressed_tuples < 0:
        parser.error("--max-decompressed-tuples must be non-negative")

    total = 0
    with connect(autocommit=True) as conn:
        conn.execute("SELECT set_config('timescaledb.max_tuples_decompressed_per_dml_transaction', %s, false)",
                     (str(args.max_decompressed_tuples),))
        factors = conn.execute("""SELECT factor_id,factor_name,direction,expression,version
          FROM factor.definitions WHERE lifecycle_status=ANY(%s) ORDER BY factor_name""", (statuses,)).fetchall()
        for factor in factors:
            # Serialize this builder per factor so the previous-row archive and the
            # replacement describe one transition even if two schedules overlap.
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext('factor-score-contract-v1'),hashtext(%s))",
                             (str(factor['factor_id']),))
                result=conn.execute(build_score_sql(compile_expression(factor['expression'])), {
                    "factor_id": factor["factor_id"], "factor_version": factor["version"],
                    "direction": factor["direction"], "since": since, "until": until,
                    "stock_code": args.stock_code, "min_cohort_size": max(2, args.min_cohort_size),
                }).fetchone()
            total += result['changed_rows']
            print(f"{factor['factor_name']}: changed={result['changed_rows']} invalid={result['invalid_rows']} archived={result['archived_rows']}", flush=True)
    print(f"factors={len(factors)} total_changed_rows={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
