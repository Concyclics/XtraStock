#!/usr/bin/env python3
"""Refresh small per-factor daily health summaries for Agent queries."""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect


HEALTH_SQL = """
                WITH cohort_versions AS (
                  SELECT f.dt,md5(string_agg(f.asset_id::text||':'||extract(epoch FROM f.updated_at)::text,
                      ',' ORDER BY f.asset_id)) AS source_cohort_hash
                  FROM factor.feature_daily f JOIN market.assets a USING(asset_id)
                  WHERE f.dt BETWEEN %(since)s AND %(until)s AND a.asset_type='stock'
                    AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
                  GROUP BY f.dt
                ), eligible AS (
                  -- Coverage of the identified stock inventory, not proof that
                  -- every member is historically tradable. Execution checks
                  -- lifecycle versions separately.
                  SELECT count(*)::int AS asset_count FROM market.assets
                  WHERE asset_type='stock' AND NOT coalesce(metadata ? 'identity_alias_of',false)
                ), checked AS (
                  SELECT s.*,d.lifecycle_status,
                    coalesce(s.quality_status='valid' AND s.input_contract_version='pit-input-v1'
                      AND s.raw_value IS NOT NULL AND s.raw_value NOT IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
                      AND s.source_factor_version=d.version
                      AND s.source_feature_updated_at=f.updated_at
                      AND s.source_cohort_hash=cv.source_cohort_hash
                      AND s.source_decision_cutoff=f.decision_cutoff
                      AND f.input_contract_version='pit-input-v1' AND f.knowledge_mode='system'
                      AND f.input_quality->>'strict_pit_ready'='true'
                      AND f.available_at<=f.decision_cutoff AND f.decision_cutoff<=statement_timestamp()
                      AND f.decision_cutoff<((f.dt+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
                      AND s.source_feature_date=f.dt
                      AND s.available_at=f.available_at
                      AND a.asset_type='stock' AND NOT coalesce(a.metadata ? 'identity_alias_of',false),false) AS usable
                  FROM factor.daily_scores s JOIN factor.definitions d USING(factor_id)
                  LEFT JOIN factor.feature_daily f USING(asset_id,dt)
                  LEFT JOIN cohort_versions cv USING(dt)
                  LEFT JOIN market.assets a USING(asset_id)
                  WHERE s.dt BETWEEN %(since)s AND %(until)s
                ), aggregated AS (
                  SELECT factor_id,dt,lifecycle_status,
                         count(*) FILTER(WHERE usable)::bigint AS score_rows,
                         count(DISTINCT asset_id) FILTER(WHERE usable)::int AS scored_assets,
                         count(*) FILTER (WHERE usable AND raw_value=0)::double precision
                           / nullif(count(*) FILTER(WHERE usable),0) AS zero_value_ratio,
                         avg(raw_value) FILTER(WHERE usable) AS raw_mean,
                         stddev_samp(raw_value) FILTER(WHERE usable) AS raw_stddev,
                         min(raw_value) FILTER(WHERE usable) AS raw_min,
                         max(raw_value) FILTER(WHERE usable) AS raw_max,
                         max(available_at) FILTER(WHERE usable) AS latest_available_at,
                         max(source_feature_date) FILTER(WHERE usable) AS source_feature_date
                  FROM checked GROUP BY factor_id,dt,lifecycle_status
                )
                INSERT INTO factor.factor_health_daily (
                  factor_id,dt,lifecycle_status,score_rows,scored_assets,eligible_assets,
                  coverage,zero_value_ratio,raw_mean,raw_stddev,raw_min,raw_max,
                  latest_available_at,source_feature_date,computed_at
                )
                SELECT a.factor_id,a.dt,a.lifecycle_status,a.score_rows,a.scored_assets,e.asset_count,
                       CASE WHEN e.asset_count>0 THEN least(1.0,a.scored_assets::double precision/e.asset_count) ELSE 0 END,
                       a.zero_value_ratio,a.raw_mean,a.raw_stddev,a.raw_min,a.raw_max,
                       a.latest_available_at,a.source_feature_date,now()
                FROM aggregated a CROSS JOIN eligible e
                ON CONFLICT (factor_id,dt) DO UPDATE SET
                  lifecycle_status=EXCLUDED.lifecycle_status,
                  score_rows=EXCLUDED.score_rows,scored_assets=EXCLUDED.scored_assets,
                  eligible_assets=EXCLUDED.eligible_assets,coverage=EXCLUDED.coverage,
                  zero_value_ratio=EXCLUDED.zero_value_ratio,raw_mean=EXCLUDED.raw_mean,
                  raw_stddev=EXCLUDED.raw_stddev,raw_min=EXCLUDED.raw_min,raw_max=EXCLUDED.raw_max,
                  latest_available_at=EXCLUDED.latest_available_at,
                  source_feature_date=EXCLUDED.source_feature_date,computed_at=now()
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since must be on or before --until")

    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                HEALTH_SQL,
                {"since":since,"until":until},
            )
            rows = cur.rowcount
    print(f"factor_health since={since} until={until} upserted={rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
