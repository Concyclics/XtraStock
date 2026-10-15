#!/usr/bin/env python3
"""Refresh point-in-time availability metadata and canonical price policies."""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", help="Quality-check date; defaults to today.")
    parser.add_argument("--skip-financial-backfill", action="store_true")
    args = parser.parse_args()
    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else dt.date.today()
    counts: dict[str, int] = {}

    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            missing_assets = cur.execute(
                """SELECT a.asset_id FROM market.assets a
                   WHERE a.asset_type='stock'
                     AND NOT EXISTS (
                       SELECT 1 FROM analytics.asset_price_source_policy p
                       WHERE p.asset_id=a.asset_id
                     )"""
            ).fetchall()
            if missing_assets:
                cur.execute(
                    """
                WITH source_stats AS (
                  SELECT b.asset_id, b.source, count(*) AS row_count,
                         min(b.ts) AS min_ts, max(b.ts) AS max_ts,
                         CASE WHEN b.source='eastmoney:kline:day:qfq' THEN 1
                              WHEN b.source='eastmoney:kline:day:none' THEN 2
                              WHEN b.source='sina:kline:day:none' THEN 3 ELSE 9 END AS priority
                  FROM market.bars_daily b
                  WHERE b.asset_id=ANY(%(asset_ids)s)
                  GROUP BY b.asset_id, b.source
                ), selected AS (
                  SELECT DISTINCT ON (asset_id)
                         asset_id, source, row_count, min_ts, max_ts
                  FROM source_stats
                  WHERE row_count >= 2
                  ORDER BY asset_id, priority, row_count DESC
                )
                INSERT INTO analytics.asset_price_source_policy (
                  asset_id, price_source, adjustment, metadata, last_verified_at
                )
                SELECT asset_id, source,
                       CASE WHEN source LIKE '%%:qfq' THEN 'qfq' ELSE 'none' END,
                       jsonb_build_object('row_count_at_selection', row_count,
                                          'min_ts', min_ts, 'max_ts', max_ts), now()
                FROM selected
                ON CONFLICT (asset_id) DO NOTHING
                    """,
                    {"asset_ids": [row["asset_id"] for row in missing_assets]},
                )
                counts["price_policies"] = cur.rowcount
            else:
                counts["price_policies"] = 0

            cur.execute(
                """UPDATE market.factors_daily
                   SET available_at=(ts::timestamp + time '15:05') AT TIME ZONE 'Asia/Shanghai'
                   WHERE available_at IS NULL"""
            )
            counts["factors_available"] = cur.rowcount
            cur.execute(
                """
                UPDATE research.reports r
                SET available_at=coalesce(
                  d.published_at,
                  (r.report_date::timestamp + interval '1 day') AT TIME ZONE 'Asia/Shanghai',
                  r.created_at)
                FROM corpus.documents d
                WHERE d.id=r.document_id AND r.available_at IS NULL
                """
            )
            counts["reports_available"] = cur.rowcount
            cur.execute(
                """
                UPDATE research.reports r
                SET available_at=coalesce(
                  (r.report_date::timestamp + interval '1 day') AT TIME ZONE 'Asia/Shanghai',
                  r.created_at)
                WHERE r.available_at IS NULL
                """
            )
            counts["reports_fallback"] = cur.rowcount
            cur.execute(
                """
                UPDATE company.financial_statement_metrics m
                SET available_at=coalesce(
                  d.published_at,
                  (m.report_date::timestamp + interval '1 day') AT TIME ZONE 'Asia/Shanghai',
                  m.created_at)
                FROM corpus.documents d
                WHERE d.id=m.source_document_id AND m.available_at IS NULL
                """
            )
            counts["statements_available"] = cur.rowcount

            if not args.skip_financial_backfill:
                cur.execute(
                    """
                    WITH announcements AS (
                      SELECT DISTINCT ON (asset_id, event_date)
                             asset_id, event_date,
                             CASE WHEN coalesce(raw_payload->>'NOTICE_DATE','') ~ '^\\d{4}-\\d{2}-\\d{2}'
                               THEN (((raw_payload->>'NOTICE_DATE')::timestamp::date + 1)::timestamp
                                     AT TIME ZONE 'Asia/Shanghai') END AS available_at,
                             CASE WHEN coalesce(raw_payload->>'UPDATE_DATE','') ~ '^\\d{4}-\\d{2}-\\d{2}'
                               THEN (raw_payload->>'UPDATE_DATE')::timestamp AT TIME ZONE 'Asia/Shanghai'
                             END AS source_updated_at
                      FROM market.events_raw
                      WHERE category='financials_raw'
                      ORDER BY asset_id, event_date, created_at DESC
                    )
                    UPDATE company.financial_metrics f
                    SET available_at=coalesce(a.available_at, f.created_at),
                        source_updated_at=a.source_updated_at
                    FROM announcements a
                    WHERE f.asset_id=a.asset_id AND f.report_date=a.event_date
                      AND (f.available_at IS NULL OR f.source_updated_at IS NULL)
                    """
                )
                counts["financials_announcement"] = cur.rowcount
                cur.execute(
                    "UPDATE company.financial_metrics SET available_at=created_at WHERE available_at IS NULL"
                )
                counts["financials_fallback"] = cur.rowcount

            cur.execute(
                """
                INSERT INTO analytics.data_quality_daily
                  (check_date, check_name, severity, status, observed_value,
                   threshold_value, affected_rows, details)
                SELECT %(as_of)s, 'canonical_price_policy_coverage', 'error',
                       CASE WHEN coverage >= 0.98 THEN 'pass'
                            WHEN coverage >= 0.95 THEN 'warn' ELSE 'fail' END,
                       coverage, 0.98, eligible-covered,
                       jsonb_build_object('eligible_assets', eligible, 'covered_assets', covered)
                FROM (
                  SELECT eligible, covered,
                         CASE WHEN eligible=0 THEN 0 ELSE covered::double precision/eligible END AS coverage
                  FROM (
                    SELECT count(*) AS eligible
                    FROM market.assets
                    WHERE asset_type='stock'
                      AND coalesce((metadata->>'is_active')::boolean, true)
                      AND coalesce(name, '') !~ '^退市'
                  ) e,
                  (
                    SELECT count(*) AS covered
                    FROM analytics.asset_price_source_policy p
                    JOIN market.assets a ON a.asset_id=p.asset_id
                    WHERE a.asset_type='stock'
                      AND coalesce((a.metadata->>'is_active')::boolean, true)
                      AND coalesce(a.name, '') !~ '^退市'
                  ) c
                ) q
                ON CONFLICT (check_date, check_name) DO UPDATE SET
                  severity=EXCLUDED.severity, status=EXCLUDED.status,
                  observed_value=EXCLUDED.observed_value,
                  threshold_value=EXCLUDED.threshold_value,
                  affected_rows=EXCLUDED.affected_rows,
                  details=EXCLUDED.details, checked_at=now()
                """,
                {"as_of": as_of},
            )
            cur.execute(
                """
                INSERT INTO analytics.data_quality_daily
                  (check_date, check_name, severity, status, observed_value,
                   threshold_value, affected_rows, details)
                WITH latest_date AS (
                  SELECT max(dt) AS dt
                  FROM market.canonical_bars_daily
                  WHERE dt BETWEEN %(as_of)s::date - 14 AND %(as_of)s::date
                ), eligible AS (
                  SELECT count(*) AS total, l.dt
                  FROM latest_date l
                  JOIN market.assets a ON a.asset_type='stock'
                    AND coalesce((a.metadata->>'is_active')::boolean, true)
                    AND coalesce(a.name, '') !~ '^退市'
                    AND (a.listed_date IS NULL OR a.listed_date <= l.dt)
                    AND EXISTS (
                      SELECT 1 FROM market.canonical_bars_daily recent
                      WHERE recent.asset_id=a.asset_id
                        AND recent.dt BETWEEN l.dt - 30 AND l.dt
                    )
                  GROUP BY l.dt
                ), coverage AS (
                  SELECT eligible.total, count(DISTINCT b.asset_id) AS covered, l.dt,
                         count(*) FILTER (WHERE b.is_fallback) AS fallback_rows
                  FROM latest_date l
                  JOIN eligible ON eligible.dt=l.dt
                  LEFT JOIN market.canonical_bars_daily b ON b.dt=l.dt
                  LEFT JOIN market.assets a ON a.asset_id=b.asset_id AND a.asset_type='stock'
                  WHERE b.asset_id IS NULL OR a.asset_id IS NOT NULL
                  GROUP BY eligible.total, l.dt
                )
                SELECT %(as_of)s, 'canonical_price_latest_coverage', 'error',
                       CASE WHEN covered::double precision/nullif(total,0) >= 0.95 THEN 'pass'
                            WHEN covered::double precision/nullif(total,0) >= 0.85 THEN 'warn'
                            ELSE 'fail' END,
                       covered::double precision/nullif(total,0), 0.95, total-covered,
                       jsonb_build_object('latest_market_date', dt,
                                          'covered_assets', covered, 'eligible_assets', total,
                                          'fallback_rows', fallback_rows)
                FROM coverage
                ON CONFLICT (check_date, check_name) DO UPDATE SET
                  severity=EXCLUDED.severity, status=EXCLUDED.status,
                  observed_value=EXCLUDED.observed_value,
                  threshold_value=EXCLUDED.threshold_value,
                  affected_rows=EXCLUDED.affected_rows,
                  details=EXCLUDED.details, checked_at=now()
                """,
                {"as_of": as_of},
            )
            if not args.skip_financial_backfill:
                cur.execute(
                    """
                    INSERT INTO analytics.data_quality_daily
                      (check_date, check_name, severity, status, observed_value,
                       threshold_value, affected_rows, details)
                    SELECT %(as_of)s, 'financial_availability_coverage', 'warning',
                           CASE WHEN coverage >= 0.99 THEN 'pass'
                                WHEN coverage >= 0.95 THEN 'warn' ELSE 'fail' END,
                           coverage, 0.99, missing, jsonb_build_object('rows', total)
                    FROM (
                      SELECT count(*) AS total,
                             count(*) FILTER (WHERE available_at IS NULL) AS missing,
                             CASE WHEN count(*)=0 THEN 0 ELSE
                               count(available_at)::double precision/count(*) END AS coverage
                      FROM company.financial_metrics
                    ) q
                    ON CONFLICT (check_date, check_name) DO UPDATE SET
                      severity=EXCLUDED.severity, status=EXCLUDED.status,
                      observed_value=EXCLUDED.observed_value,
                      threshold_value=EXCLUDED.threshold_value,
                      affected_rows=EXCLUDED.affected_rows,
                      details=EXCLUDED.details, checked_at=now()
                    """,
                    {"as_of": as_of},
                )
        conn.commit()

    print(" ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
