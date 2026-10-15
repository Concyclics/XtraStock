#!/usr/bin/env python3
"""Build a conservative point-in-time asset lifecycle baseline."""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from common import connect

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from v3_ops import record_quality, set_dataset_watermark, stage_run  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronize current lifecycle ranges from market.assets.")
    parser.add_argument("--as-of", default=dt.date.today().isoformat())
    args = parser.parse_args()
    as_of = dt.date.fromisoformat(args.as_of)

    with stage_run("finance_v3_reference", "asset_lifecycle", expected_date=as_of) as run:
        with connect(autocommit=False, admin=False) as conn:
            row = conn.execute(
                """
                WITH bar_stats AS (
                  SELECT asset_id,
                         min((ts AT TIME ZONE 'Asia/Shanghai')::date) AS first_dt,
                         max((ts AT TIME ZONE 'Asia/Shanghai')::date) AS last_dt,
                         bool_or(
                           ts >= (%(as_of)s::date::timestamp AT TIME ZONE 'Asia/Shanghai')
                           AND ts < ((%(as_of)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
                         ) AS has_asof_bar,
                         min((ts AT TIME ZONE 'Asia/Shanghai')::date) FILTER (
                           WHERE ts >= ((%(as_of)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
                         ) AS next_dt
                  FROM market.bars_daily
                  GROUP BY asset_id
                ), classified AS (
                  SELECT a.asset_id,a.asset_type,a.name,a.exchange,
                         coalesce(a.listed_date,b.first_dt,DATE '1990-01-01') AS start_dt,
                         b.first_dt,b.last_dt,b.next_dt,coalesce(b.has_asof_bar,false) AS has_asof_bar,
                         coalesce((a.metadata->>'is_st')::boolean,false)
                           OR coalesce(a.name,'') ~* '(^|[^A-Z])\\*?ST' AS is_st,
                         CASE
                           WHEN a.listed_date IS NOT NULL AND a.listed_date > %(as_of)s THEN 'pre_listed'
                           WHEN a.delisted_date IS NOT NULL AND a.delisted_date <= %(as_of)s THEN 'delisted'
                           WHEN coalesce(a.lifecycle_status,'active')='delisted' THEN 'delisted'
                           WHEN coalesce((a.metadata->>'is_active')::boolean,true)=false THEN 'delisted'
                           WHEN coalesce(a.name,'') ~ '(退市|退$|定转$)' THEN 'delisted'
                           WHEN b.first_dt IS NULL THEN 'unknown'
                           WHEN b.last_dt < %(as_of)s - 365 THEN 'delisted'
                           WHEN coalesce(b.has_asof_bar,false) THEN 'active'
                           WHEN b.last_dt >= %(as_of)s - 10
                             AND b.next_dt IS NOT NULL AND b.next_dt <= %(as_of)s + 10
                           THEN 'suspended'
                           WHEN b.last_dt >= %(as_of)s - 10
                             AND b.next_dt IS NULL
                             AND coalesce((a.metadata->'raw'->>'total_mv')::numeric,0) > 0
                           THEN 'suspended'
                           ELSE 'unknown'
                         END AS state,
                         CASE
                           WHEN a.delisted_date IS NOT NULL THEN a.delisted_date
                           WHEN coalesce(a.lifecycle_status,'active')='delisted'
                             OR coalesce((a.metadata->>'is_active')::boolean,true)=false
                             OR coalesce(a.name,'') ~ '(退市|退$|定转$)'
                             OR b.last_dt < %(as_of)s - 365
                           THEN b.last_dt
                         END AS inferred_delisted_dt
                  FROM market.assets a
                  LEFT JOIN bar_stats b USING(asset_id)
                  WHERE a.asset_type IN ('stock','index','fund')
                    AND NOT (a.metadata ? 'identity_alias_of')
                ), source_rows AS (
                  -- Historical active interval before a known delisting or a suspension snapshot.
                  SELECT asset_id,start_dt AS valid_from,
                         CASE WHEN state='delisted' THEN inferred_delisted_dt
                              WHEN state='suspended' THEN %(as_of)s - 1
                              WHEN state='unknown' THEN last_dt
                              ELSE NULL END AS valid_to,
                         'active'::text AS lifecycle_status,true AS is_tradable,
                         is_st,false AS is_suspended,upper(coalesce(exchange,'')) AS exchange,
                         state AS inferred_state
                  FROM classified
                  WHERE first_dt IS NOT NULL AND state <> 'pre_listed'
                    AND (state NOT IN ('delisted','suspended','unknown') OR start_dt <= coalesce(
                      CASE WHEN state='delisted' THEN inferred_delisted_dt
                           WHEN state='unknown' THEN last_dt ELSE %(as_of)s - 1 END,
                      start_dt-1
                    ))
                  UNION ALL
                  SELECT asset_id,%(as_of)s,
                         CASE WHEN next_dt IS NOT NULL THEN next_dt-1 END,
                         'suspended',false,is_st,true,upper(coalesce(exchange,'')),state
                  FROM classified WHERE state='suspended'
                  UNION ALL
                  SELECT asset_id,next_dt,NULL,'active',true,is_st,false,
                         upper(coalesce(exchange,'')),state
                  FROM classified WHERE state='suspended' AND next_dt IS NOT NULL
                  UNION ALL
                  SELECT asset_id,coalesce(inferred_delisted_dt+1,%(as_of)s),NULL,
                         'delisted',false,is_st,false,upper(coalesce(exchange,'')),state
                  FROM classified WHERE state='delisted'
                  UNION ALL
                  SELECT asset_id,coalesce(last_dt+1,%(as_of)s),NULL,'unknown',false,is_st,false,
                         upper(coalesce(exchange,'')),state
                  FROM classified WHERE state='unknown'
                  UNION ALL
                  SELECT c.asset_id,%(as_of)s,a.listed_date-1,'pre_listed',false,c.is_st,false,
                         upper(coalesce(c.exchange,'')),c.state
                  FROM classified c
                  JOIN market.assets a USING(asset_id)
                  WHERE state='pre_listed'
                  UNION ALL
                  SELECT c.asset_id,a.listed_date,NULL,'active',true,c.is_st,false,
                         upper(coalesce(c.exchange,'')),c.state
                  FROM classified c
                  JOIN market.assets a USING(asset_id)
                  WHERE state='pre_listed'
                ), deleted AS (
                  DELETE FROM market.asset_lifecycle_versions v
                  WHERE v.source_name IN ('market.assets baseline:v1','market.assets baseline:v2')
                  RETURNING v.asset_id
                ), inserted AS (
                  INSERT INTO market.asset_lifecycle_versions (
                    asset_id,valid_from,valid_to,lifecycle_status,is_tradable,is_st,
                    is_suspended,exchange,source_name,available_at,quality_status,metadata
                  )
                  SELECT asset_id,valid_from,valid_to,lifecycle_status,is_tradable,is_st,
                         is_suspended,nullif(exchange,''),'market.assets baseline:v2',now(),
                         'unverified',jsonb_build_object(
                           'as_of',%(as_of)s,'inferred_state',inferred_state,
                           'pit_limit','bar-presence and current metadata; official history pending'
                         )
                  FROM source_rows
                  WHERE valid_to IS NULL OR valid_to >= valid_from
                  RETURNING asset_id,is_tradable,lifecycle_status
                )
                SELECT count(*) AS rows,
                       count(*) FILTER (WHERE is_tradable) AS tradable,
                       count(*) FILTER (WHERE lifecycle_status='delisted') AS delisted,
                       count(*) FILTER (WHERE lifecycle_status='unknown') AS unknown
                FROM inserted
                """,
                {"as_of": as_of},
            ).fetchone()
            conn.commit()
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
            source_version="market.assets baseline:v2",
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
