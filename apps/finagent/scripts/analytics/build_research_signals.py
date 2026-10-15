# -*- coding: utf-8 -*-
"""Build daily research-report momentum aggregates."""
from __future__ import annotations

import argparse

from common import connect


RATING_SCORE_SQL = """
CASE
  WHEN rating ~ '买入|强烈推荐|推荐' THEN 5.0
  WHEN rating ~ '增持|优于|跑赢|谨慎推荐' THEN 4.0
  WHEN rating ~ '持有|中性|同步|与大市同步' THEN 3.0
  WHEN rating ~ '减持|卖出|弱于|跑输' THEN 1.0
  ELSE NULL
END
"""


def build_asset_sql() -> str:
    return f"""
WITH base AS (
  SELECT
    r.document_id,
    r.report_date AS dt,
    NULLIF(trim(r.stock_code), '') AS stock_code,
    NULLIF(trim(r.stock_name), '') AS stock_name,
    NULLIF(trim(r.institution), '') AS institution,
    COALESCE(r.rating, '') AS rating,
    COALESCE(r.rating_change, '') AS rating_change,
    {RATING_SCORE_SQL} AS rating_score
  FROM research.reports_resolved r
  WHERE r.report_type = 'stock'
    AND r.report_date IS NOT NULL
    AND NULLIF(trim(r.stock_code), '') IS NOT NULL
    AND (%(since)s::date IS NULL OR r.report_date >= %(since)s::date)
    AND (%(until)s::date IS NULL OR r.report_date <= %(until)s::date)
    AND greatest(coalesce(r.available_at, r.created_at),r.identity_known_at) <=
        ((r.report_date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
),
asset_lookup AS (
  SELECT symbol AS stock_code, min(asset_id::text)::uuid AS asset_id
  FROM market.assets
  WHERE asset_type = 'stock'
  GROUP BY symbol
),
inst AS (
  SELECT stock_code, dt, institution, count(*) AS cnt
  FROM base
  WHERE institution IS NOT NULL
  GROUP BY stock_code, dt, institution
),
inst_json AS (
  SELECT stock_code, dt, jsonb_object_agg(institution, cnt ORDER BY cnt DESC, institution) AS institution_counts
  FROM inst
  GROUP BY stock_code, dt
),
agg AS (
  SELECT
    b.stock_code,
    max(b.stock_name) FILTER (WHERE b.stock_name IS NOT NULL) AS stock_name,
    b.dt,
    count(*)::int AS report_count,
    count(DISTINCT b.institution)::int AS distinct_institution_count,
    count(*) FILTER (WHERE b.rating LIKE '%%买入%%')::int AS buy_count,
    count(*) FILTER (WHERE b.rating LIKE '%%增持%%')::int AS overweight_count,
    count(*) FILTER (WHERE b.rating ~ '买入|强烈推荐|推荐|增持|优于|跑赢|谨慎推荐')::int AS positive_rating_count,
    count(*) FILTER (WHERE b.rating_change ~ '调高|上调|提高|升')::int AS upgrade_count,
    count(*) FILTER (WHERE b.rating_change ~ '调低|下调|降低|降')::int AS downgrade_count,
    count(*) FILTER (WHERE b.rating_change ~ '首次|首予|初次')::int AS first_coverage_count,
    avg(b.rating_score)::double precision AS rating_score_avg,
    max(b.rating_score)::double precision AS rating_score_max,
    array_agg(b.document_id ORDER BY b.document_id) AS source_document_ids
  FROM base b
  GROUP BY b.stock_code, b.dt
)
INSERT INTO analytics.asset_research_daily (
  asset_id, stock_code, stock_name, dt, report_count, distinct_institution_count,
  buy_count, overweight_count, positive_rating_count, upgrade_count, downgrade_count,
  first_coverage_count, rating_score_avg, rating_score_max, institution_counts,
  source_document_ids, updated_at
)
SELECT
  al.asset_id, a.stock_code, a.stock_name, a.dt, a.report_count, a.distinct_institution_count,
  a.buy_count, a.overweight_count, a.positive_rating_count, a.upgrade_count, a.downgrade_count,
  a.first_coverage_count, a.rating_score_avg, a.rating_score_max,
  COALESCE(ij.institution_counts, '{{}}'::jsonb),
  a.source_document_ids, now()
FROM agg a
LEFT JOIN asset_lookup al ON al.stock_code = a.stock_code
LEFT JOIN inst_json ij ON ij.stock_code = a.stock_code AND ij.dt = a.dt
ON CONFLICT (stock_code, dt) DO UPDATE SET
  asset_id = EXCLUDED.asset_id,
  stock_name = EXCLUDED.stock_name,
  report_count = EXCLUDED.report_count,
  distinct_institution_count = EXCLUDED.distinct_institution_count,
  buy_count = EXCLUDED.buy_count,
  overweight_count = EXCLUDED.overweight_count,
  positive_rating_count = EXCLUDED.positive_rating_count,
  upgrade_count = EXCLUDED.upgrade_count,
  downgrade_count = EXCLUDED.downgrade_count,
  first_coverage_count = EXCLUDED.first_coverage_count,
  rating_score_avg = EXCLUDED.rating_score_avg,
  rating_score_max = EXCLUDED.rating_score_max,
  institution_counts = EXCLUDED.institution_counts,
  source_document_ids = EXCLUDED.source_document_ids,
  updated_at = now()
"""


def build_industry_sql() -> str:
    return f"""
WITH base AS (
  SELECT
    r.document_id,
    r.report_date AS dt,
    NULLIF(trim(COALESCE(r.industry_name, r.stock_name)), '') AS industry_name,
    NULLIF(trim(r.institution), '') AS institution,
    COALESCE(r.rating, '') AS rating,
    COALESCE(r.rating_change, '') AS rating_change,
    {RATING_SCORE_SQL} AS rating_score
  FROM research.reports_resolved r
  WHERE r.report_type = 'industry'
    AND r.report_date IS NOT NULL
    AND NULLIF(trim(COALESCE(r.industry_name, r.stock_name)), '') IS NOT NULL
    AND (%(since)s::date IS NULL OR r.report_date >= %(since)s::date)
    AND (%(until)s::date IS NULL OR r.report_date <= %(until)s::date)
    AND greatest(coalesce(r.available_at, r.created_at),r.identity_known_at) <=
        ((r.report_date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
),
inst AS (
  SELECT industry_name, dt, institution, count(*) AS cnt
  FROM base
  WHERE institution IS NOT NULL
  GROUP BY industry_name, dt, institution
),
inst_json AS (
  SELECT industry_name, dt, jsonb_object_agg(institution, cnt ORDER BY cnt DESC, institution) AS institution_counts
  FROM inst
  GROUP BY industry_name, dt
),
agg AS (
  SELECT
    b.industry_name,
    b.dt,
    count(*)::int AS report_count,
    count(DISTINCT b.institution)::int AS distinct_institution_count,
    count(*) FILTER (WHERE b.rating LIKE '%%买入%%')::int AS buy_count,
    count(*) FILTER (WHERE b.rating LIKE '%%增持%%')::int AS overweight_count,
    count(*) FILTER (WHERE b.rating ~ '买入|强烈推荐|推荐|增持|优于|跑赢|谨慎推荐')::int AS positive_rating_count,
    count(*) FILTER (WHERE b.rating_change ~ '调高|上调|提高|升')::int AS upgrade_count,
    count(*) FILTER (WHERE b.rating_change ~ '调低|下调|降低|降')::int AS downgrade_count,
    count(*) FILTER (WHERE b.rating_change ~ '首次|首予|初次')::int AS first_coverage_count,
    avg(b.rating_score)::double precision AS rating_score_avg,
    max(b.rating_score)::double precision AS rating_score_max,
    array_agg(b.document_id ORDER BY b.document_id) AS source_document_ids
  FROM base b
  GROUP BY b.industry_name, b.dt
)
INSERT INTO analytics.industry_research_daily (
  industry_name, dt, report_count, distinct_institution_count, buy_count,
  overweight_count, positive_rating_count, upgrade_count, downgrade_count,
  first_coverage_count, rating_score_avg, rating_score_max, institution_counts,
  source_document_ids, updated_at
)
SELECT
  a.industry_name, a.dt, a.report_count, a.distinct_institution_count, a.buy_count,
  a.overweight_count, a.positive_rating_count, a.upgrade_count, a.downgrade_count,
  a.first_coverage_count, a.rating_score_avg, a.rating_score_max,
  COALESCE(ij.institution_counts, '{{}}'::jsonb), a.source_document_ids, now()
FROM agg a
LEFT JOIN inst_json ij ON ij.industry_name = a.industry_name AND ij.dt = a.dt
ON CONFLICT (industry_name, dt) DO UPDATE SET
  report_count = EXCLUDED.report_count,
  distinct_institution_count = EXCLUDED.distinct_institution_count,
  buy_count = EXCLUDED.buy_count,
  overweight_count = EXCLUDED.overweight_count,
  positive_rating_count = EXCLUDED.positive_rating_count,
  upgrade_count = EXCLUDED.upgrade_count,
  downgrade_count = EXCLUDED.downgrade_count,
  first_coverage_count = EXCLUDED.first_coverage_count,
  rating_score_avg = EXCLUDED.rating_score_avg,
  rating_score_max = EXCLUDED.rating_score_max,
  institution_counts = EXCLUDED.institution_counts,
  source_document_ids = EXCLUDED.source_document_ids,
  updated_at = now()
"""


def build_institution_sql() -> str:
    return f"""
WITH base AS (
  SELECT
    r.document_id,
    r.report_date AS dt,
    NULLIF(trim(r.institution), '') AS institution,
    CASE WHEN r.report_type = 'stock' THEN 'stock' ELSE 'industry' END AS target_type,
    CASE WHEN r.report_type = 'stock' THEN NULLIF(trim(r.stock_code), '') ELSE NULLIF(trim(COALESCE(r.industry_name, r.stock_name)), '') END AS target_key,
    CASE WHEN r.report_type = 'stock' THEN NULLIF(trim(r.stock_name), '') ELSE NULLIF(trim(COALESCE(r.industry_name, r.stock_name)), '') END AS target_name,
    COALESCE(r.rating, '') AS rating,
    COALESCE(r.rating_change, '') AS rating_change,
    {RATING_SCORE_SQL} AS rating_score
  FROM research.reports_resolved r
  WHERE r.report_type IN ('stock', 'industry')
    AND r.report_date IS NOT NULL
    AND NULLIF(trim(r.institution), '') IS NOT NULL
    AND (%(since)s::date IS NULL OR r.report_date >= %(since)s::date)
    AND (%(until)s::date IS NULL OR r.report_date <= %(until)s::date)
    AND greatest(coalesce(r.available_at, r.created_at),r.identity_known_at) <=
        ((r.report_date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
),
agg AS (
  SELECT
    institution, target_type, target_key,
    max(target_name) FILTER (WHERE target_name IS NOT NULL) AS target_name,
    dt,
    count(*)::int AS report_count,
    count(*) FILTER (WHERE rating ~ '买入|强烈推荐|推荐|增持|优于|跑赢|谨慎推荐')::int AS positive_rating_count,
    count(*) FILTER (WHERE rating_change ~ '调高|上调|提高|升')::int AS upgrade_count,
    count(*) FILTER (WHERE rating_change ~ '调低|下调|降低|降')::int AS downgrade_count,
    count(*) FILTER (WHERE rating_change ~ '首次|首予|初次')::int AS first_coverage_count,
    avg(rating_score)::double precision AS rating_score_avg,
    array_agg(document_id ORDER BY document_id) AS source_document_ids
  FROM base
  WHERE target_key IS NOT NULL
  GROUP BY institution, target_type, target_key, dt
)
INSERT INTO analytics.institution_research_daily (
  institution, target_type, target_key, target_name, dt, report_count,
  positive_rating_count, upgrade_count, downgrade_count, first_coverage_count,
  rating_score_avg, source_document_ids, updated_at
)
SELECT
  institution, target_type, target_key, target_name, dt, report_count,
  positive_rating_count, upgrade_count, downgrade_count, first_coverage_count,
  rating_score_avg, source_document_ids, now()
FROM agg
ON CONFLICT (institution, target_type, target_key, dt) DO UPDATE SET
  target_name = EXCLUDED.target_name,
  report_count = EXCLUDED.report_count,
  positive_rating_count = EXCLUDED.positive_rating_count,
  upgrade_count = EXCLUDED.upgrade_count,
  downgrade_count = EXCLUDED.downgrade_count,
  first_coverage_count = EXCLUDED.first_coverage_count,
  rating_score_avg = EXCLUDED.rating_score_avg,
  source_document_ids = EXCLUDED.source_document_ids,
  updated_at = now()
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help="Inclusive report date, YYYY-MM-DD.")
    parser.add_argument("--until", help="Inclusive report date, YYYY-MM-DD.")
    args = parser.parse_args()

    params = {"since": args.since, "until": args.until}
    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            for label, sql in (
                ("asset_research_daily", build_asset_sql()),
                ("industry_research_daily", build_industry_sql()),
                ("institution_research_daily", build_institution_sql()),
            ):
                cur.execute(sql, params)
                print(f"{label}: upserted {cur.rowcount}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
