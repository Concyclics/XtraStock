#!/usr/bin/env python3
"""Materialize one finalized, explicitly sourced daily bar per asset/date."""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect


SELECT_SQL = """
CREATE TEMP TABLE canonical_daily_selected ON COMMIT DROP AS
WITH candidates AS (
  SELECT
    b.asset_id,
    (b.ts AT TIME ZONE 'Asia/Shanghai')::date AS dt,
    b.open, b.high, b.low, b.close,
    CASE WHEN b.source = 'sina:kline:day:none' THEN b.volume / 100.0 ELSE b.volume END AS volume,
    b.amount,
    b.source,
    CASE
      WHEN b.source = 'eastmoney:kline:day:qfq' THEN 'qfq'
      ELSE 'none'
    END AS adjustment,
    b.ts AS source_ts,
    coalesce(b.updated_at, b.created_at) AS source_updated_at,
    CASE b.source
      WHEN 'eastmoney:kline:day:qfq' THEN 1
      WHEN 'eastmoney:kline:day:none' THEN 2
      WHEN 'sina:kline:day:none' THEN 3
      ELSE 99
    END AS source_priority
  FROM market.bars_daily b
  JOIN market.assets a ON a.asset_id=b.asset_id
  WHERE a.asset_type = ANY(%(asset_types)s)
    AND b.source IN (
      'eastmoney:kline:day:qfq',
      'eastmoney:kline:day:none',
      'sina:kline:day:none'
    )
    AND b.ts >= (%(since)s::date::timestamp AT TIME ZONE 'Asia/Shanghai')
    AND b.ts < ((%(until)s::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
    AND (b.ts AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %(since)s AND %(until)s
    AND coalesce(b.updated_at, b.created_at) >= (
      ((b.ts AT TIME ZONE 'Asia/Shanghai')::date::timestamp + time '15:05')
      AT TIME ZONE 'Asia/Shanghai'
    )
    AND b.close IS NOT NULL
), ranked AS (
  SELECT c.*, row_number() OVER (
    PARTITION BY c.asset_id, c.dt
    ORDER BY c.source_priority, c.source_updated_at DESC
  ) AS rn
  FROM candidates c
)
SELECT
  asset_id, dt, open, high, low, close, volume, amount, source, adjustment,
  source_ts, source_updated_at,
  source <> 'eastmoney:kline:day:qfq' AS is_fallback,
  (dt::timestamp + time '15:05') AT TIME ZONE 'Asia/Shanghai' AS available_at,
  jsonb_strip_nulls(jsonb_build_object(
    'volume_unit', 'lot',
    'source_priority', source_priority,
    'fallback_reason', CASE
      WHEN source_priority > 1 THEN 'preferred_eastmoney_qfq_unavailable'
    END
  )) AS quality
FROM ranked
WHERE rn=1
"""

DELETE_SQL = """
DELETE FROM market.canonical_bars_daily
WHERE dt BETWEEN %(since)s AND %(until)s
  AND asset_id IN (
    SELECT asset_id FROM market.assets WHERE asset_type = ANY(%(asset_types)s)
  )
"""

INSERT_SQL = """
INSERT INTO market.canonical_bars_daily (
  asset_id, dt, open, high, low, close, volume, amount, source, adjustment,
  source_ts, source_updated_at, is_fallback, available_at, quality, updated_at
)
SELECT
  asset_id, dt, open, high, low, close, volume, amount, source, adjustment,
  source_ts, source_updated_at, is_fallback, available_at, quality, now()
FROM canonical_daily_selected
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--asset-types", default="stock,index")
    parser.add_argument(
        "--min-assets",
        type=int,
        default=0,
        help="Abort when the latest selected trading date has fewer distinct assets.",
    )
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since must not be after --until")
    params = {
        "since": since,
        "until": until,
        "asset_types": [x.strip() for x in args.asset_types.split(",") if x.strip()],
    }

    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(SELECT_SQL, params)
            cur.execute(
                """
                SELECT dt, count(DISTINCT asset_id) AS assets,
                       count(*) FILTER (WHERE is_fallback) AS fallback
                FROM canonical_daily_selected
                GROUP BY dt ORDER BY dt DESC LIMIT 1
                """
            )
            latest = cur.fetchone()
            if not latest:
                raise RuntimeError("no finalized daily bars selected")
            if args.min_assets and latest["assets"] < args.min_assets:
                raise RuntimeError(
                    f"canonical coverage below threshold: dt={latest['dt']} "
                    f"assets={latest['assets']} min={args.min_assets}"
                )
            cur.execute(DELETE_SQL, params)
            cur.execute(INSERT_SQL)
            cur.execute(
                """
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE is_fallback) AS fallback,
                       min(dt) AS min_dt, max(dt) AS max_dt
                FROM canonical_daily_selected
                """
            )
            summary = cur.fetchone()
        conn.commit()
    print(
        f"canonical_bars_daily: rows={summary['total']} fallback={summary['fallback']} "
        f"range={summary['min_dt']}..{summary['max_dt']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
