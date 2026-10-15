#!/usr/bin/env python3
"""Build a conservative point-in-time asset lifecycle baseline."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import uuid
import sys
from pathlib import Path

from common import connect

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from v3_ops import record_quality, set_dataset_watermark, stage_run  # noqa: E402


# Append-only inferred snapshots. A missing price does not establish a suspension
# or delisting. Actual delisting is inclusive; any gap before it remains unknown.
BASELINE_SQL = """
WITH target_assets AS MATERIALIZED (
  SELECT * FROM market.assets WHERE asset_type IN ('stock','index','fund')
    AND NOT (metadata ? 'identity_alias_of')
    AND (%(asset_ids)s::uuid[] IS NULL OR asset_id=ANY(%(asset_ids)s::uuid[]))
), observed_bars AS (
  -- Consolidated history avoids traversing old raw provider chunks. Recent raw
  -- prices cover the reference-before-canonical ordering in the daily pipeline.
  SELECT b.asset_id,b.dt FROM market.canonical_bars_daily_v2 b
  WHERE b.dt BETWEEN (%(as_of)s::date-INTERVAL '5 years')::date AND %(as_of)s
    AND b.close>0 AND b.quality_status NOT IN ('conflict','rejected','incomplete')
    AND (%(asset_ids)s::uuid[] IS NULL OR b.asset_id=ANY(%(asset_ids)s::uuid[]))
  UNION ALL
  SELECT b.asset_id,(b.ts AT TIME ZONE 'Asia/Shanghai')::date FROM market.bars_daily b
  WHERE b.ts>=((%(as_of)s::date-10)::timestamp AT TIME ZONE 'Asia/Shanghai')
    AND b.ts<((%(as_of)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
    AND b.close>0
    AND (%(asset_ids)s::uuid[] IS NULL OR b.asset_id=ANY(%(asset_ids)s::uuid[]))
), bar_stats AS (
  SELECT asset_id,min(dt) first_dt,max(dt) last_dt FROM observed_bars GROUP BY asset_id
), classified AS (
  SELECT a.asset_id,a.exchange,a.listed_date,a.delisted_date,b.first_dt,b.last_dt,
    greatest(coalesce(a.listed_date,b.first_dt,%(as_of)s::date),
      (%(as_of)s::date-INTERVAL '5 years')::date) start_dt,
    coalesce((a.metadata->>'is_st')::boolean,false)
      OR coalesce(a.name,'') ~* '(^|[^A-Z])\*?ST' AS is_st,
    CASE WHEN a.listed_date>%(as_of)s THEN 'pre_listed'
      WHEN a.delisted_date<=%(as_of)s THEN 'delisted'
      WHEN b.last_dt=%(as_of)s THEN 'active' ELSE 'unknown' END state
  FROM target_assets a LEFT JOIN bar_stats b USING(asset_id)
), source_rows AS (
  SELECT asset_id,start_dt valid_from,
    CASE WHEN state='delisted' THEN least(last_dt,delisted_date-1) ELSE last_dt END valid_to,
    'active'::text lifecycle_status,true is_tradable,is_st,false is_suspended,
    upper(coalesce(exchange,'')) exchange,state inferred_state
  FROM classified WHERE first_dt IS NOT NULL AND state<>'pre_listed'
  UNION ALL
  SELECT asset_id,coalesce(last_dt+1,%(as_of)s::date),
    CASE WHEN state='delisted' THEN delisted_date-1 ELSE %(as_of)s::date END,
    'unknown',false,is_st,false,upper(coalesce(exchange,'')),state
  FROM classified WHERE state<>'pre_listed'
  UNION ALL
  SELECT asset_id,delisted_date,NULL,'delisted',false,is_st,false,upper(coalesce(exchange,'')),state
  FROM classified WHERE state='delisted'
  UNION ALL
  SELECT asset_id,%(as_of)s::date,listed_date-1,'pre_listed',false,is_st,false,upper(coalesce(exchange,'')),state
  FROM classified WHERE state='pre_listed'
), prepared AS (
  SELECT *,jsonb_build_object('as_of',%(as_of)s::date,'inferred_state',inferred_state,
    'pit_limit','bar-presence and current metadata; official history pending',
    'missing_price_is_not_delisting',true,'is_st_verified',false,
    'observed_price_window_start',(%(as_of)s::date-INTERVAL '5 years')::date,
    'price_presence_sources','five-year canonical and ten-day raw; inferred only') metadata
  FROM source_rows WHERE valid_to IS NULL OR valid_to>=valid_from
), inserted AS (
  INSERT INTO market.asset_lifecycle_versions(asset_id,valid_from,valid_to,lifecycle_status,is_tradable,is_st,
    is_suspended,exchange,source_name,available_at,quality_status,metadata)
  SELECT s.asset_id,s.valid_from,s.valid_to,s.lifecycle_status,s.is_tradable,s.is_st,s.is_suspended,
    nullif(s.exchange,''),'market.assets baseline:v3',now(),'unverified',s.metadata
  FROM prepared s WHERE NOT EXISTS (
    SELECT 1 FROM market.asset_lifecycle_versions v WHERE v.source_name='market.assets baseline:v3'
      AND v.asset_id=s.asset_id AND v.valid_from=s.valid_from AND v.valid_to IS NOT DISTINCT FROM s.valid_to
      AND v.lifecycle_status=s.lifecycle_status AND v.is_st=s.is_st AND v.metadata=s.metadata
  ) RETURNING asset_id
)
SELECT count(*) rows,count(*) FILTER(WHERE is_tradable) tradable,
  count(*) FILTER(WHERE lifecycle_status='delisted') delisted,
  count(*) FILTER(WHERE lifecycle_status='unknown') unknown,
  (SELECT count(*) FROM inserted) inserted FROM prepared
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize current lifecycle ranges from market.assets.")
    parser.add_argument("--as-of", default=dt.date.today().isoformat())
    parser.add_argument("--asset-ids-json", type=Path, help="Scoped repair; never update whole-market watermarks.")
    args = parser.parse_args()
    asset_ids = None
    if args.asset_ids_json:
        asset_ids = [uuid.UUID(x) for x in json.loads(args.asset_ids_json.read_text())]
        if not asset_ids or len(asset_ids) != len(set(asset_ids)):
            parser.error("asset ID scope must be nonempty and unique")
    as_of = dt.date.fromisoformat(args.as_of)

    with stage_run("finance_v3_reference", "asset_lifecycle", expected_date=as_of) as run:
        with connect(autocommit=False, admin=False) as conn:
            conn.execute("SET LOCAL statement_timeout='120s'")
            conn.execute("SET LOCAL lock_timeout='5s'")
            row = conn.execute(BASELINE_SQL, {"as_of": as_of, "asset_ids": asset_ids}).fetchone()
            conn.commit()
        if asset_ids is not None:
            run["quality_status"] = "warn"
            run["output_watermarks"] = {}
            print({**dict(row), "scoped_assets": len(asset_ids), "global_watermark_updated": False})
            return 0
        rows = int(row["rows"] or 0)
        tradable = int(row["tradable"] or 0)
        unknown = int(row.get("unknown") or 0) if "unknown" in row else 0
        quality = "pass" if rows and not unknown else ("warn" if rows else "fail")
        record_quality(
            run["run_id"], "market.asset_lifecycle_versions", "baseline_population",
            status="warn" if rows else "fail",
            severity="warning" if rows else "error",
            observed_value=float(rows), threshold_value=1, affected_rows=0 if rows else 1,
            details={"baseline_is_not_suspension_history": True},
        )
        record_quality(
            run["run_id"], "market.asset_lifecycle_versions", "unknown_current_state",
            status="warn" if unknown else "pass", severity="warning",
            observed_value=float(unknown), threshold_value=0, affected_rows=unknown,
            details={
                "unknown_assets_are_not_tradable": True,
                "downstream_must_exclude_unknown_from_the_pit_universe": True,
            },
        )
        set_dataset_watermark(
            "market.asset_lifecycle_versions", schema_version="v3", run_id=run["run_id"],
            expected_date=as_of,
            data_through=dt.datetime.combine(as_of, dt.time(23, 59), dt.timezone(dt.timedelta(hours=8))),
            row_count=rows, eligible_count=rows,
            coverage_pct=100.0 if rows else 0.0, quality_status=quality,
            source_version="market.assets baseline:v3",
            details={
                "tradable": tradable, "delisted": int(row["delisted"] or 0),
                "unknown": unknown,
            },
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {"market.asset_lifecycle_versions": as_of.isoformat()}
    print({"rows": rows, "tradable": tradable, "quality_status": quality})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
