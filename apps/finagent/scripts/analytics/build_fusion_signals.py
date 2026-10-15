# -*- coding: utf-8 -*-
"""Fuse research, document event, and technical features into daily asset signals."""
from __future__ import annotations

import argparse

from common import connect


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help="Inclusive signal date, YYYY-MM-DD.")
    parser.add_argument("--until", help="Inclusive signal date, YYYY-MM-DD.")
    parser.add_argument("--alert-threshold", type=float, default=6.0)
    args = parser.parse_args()

    params = {"since": args.since, "until": args.until, "threshold": args.alert_threshold}
    signal_sql = """
WITH event_stock AS (
  SELECT
    a.asset_id,
    e.event_date AS dt,
    count(*)::int AS event_count,
    sum(COALESCE(e.impact_score, 0.5) * e.polarity)::double precision AS event_score,
    sum(CASE WHEN e.event_type IN ('policy_support', 'government_investment', 'subsidy') THEN COALESCE(e.impact_score, 0.5) * e.polarity ELSE 0 END)::double precision AS policy_score,
    array_agg(DISTINCT e.event_type) AS event_types
  FROM analytics.document_events e
  JOIN LATERAL unnest(e.stock_codes) AS sc(stock_code) ON true
  JOIN market.assets a ON a.asset_type = 'stock' AND a.symbol = sc.stock_code
  WHERE e.event_date IS NOT NULL
    AND (%(since)s::date IS NULL OR e.event_date >= %(since)s::date)
    AND (%(until)s::date IS NULL OR e.event_date <= %(until)s::date)
  GROUP BY a.asset_id, e.event_date
),
base_dates AS (
  SELECT asset_id, dt FROM analytics.asset_technical_daily
  WHERE (%(since)s::date IS NULL OR dt >= %(since)s::date)
    AND (%(until)s::date IS NULL OR dt <= %(until)s::date)
  UNION
  SELECT asset_id, dt FROM analytics.asset_research_daily
  WHERE asset_id IS NOT NULL
    AND (%(since)s::date IS NULL OR dt >= %(since)s::date)
    AND (%(until)s::date IS NULL OR dt <= %(until)s::date)
  UNION
  SELECT asset_id, dt FROM event_stock
),
joined AS (
  SELECT
    bd.asset_id,
    bd.dt,
    a.symbol AS stock_code,
    a.name AS stock_name,
    COALESCE(ar.report_count, 0) AS report_count,
    COALESCE(ar.distinct_institution_count, 0) AS institution_count,
    COALESCE(ar.positive_rating_count, 0) AS positive_rating_count,
    COALESCE(ar.upgrade_count, 0) AS upgrade_count,
    COALESCE(ar.first_coverage_count, 0) AS first_coverage_count,
    COALESCE(ar.rating_score_avg, 0) AS rating_score_avg,
    COALESCE(at.pct_chg_1d, 0) AS pct_chg_1d,
    COALESCE(at.ret_5d, 0) AS ret_5d,
    COALESCE(at.ret_20d, 0) AS ret_20d,
    COALESCE(at.volume_ratio_20d, 0) AS volume_ratio_20d,
    COALESCE(at.breakout_20d, false) AS breakout_20d,
    COALESCE(at.new_high_20d, false) AS new_high_20d,
    COALESCE(at.pattern_tags, '{}'::text[]) AS technical_tags,
    COALESCE(es.event_score, 0) AS event_score,
    COALESCE(es.policy_score, 0) AS policy_score,
    COALESCE(es.event_types, '{}'::text[]) AS event_types,
    ar.source_document_ids AS research_documents
  FROM base_dates bd
  JOIN market.assets a ON a.asset_id = bd.asset_id
  LEFT JOIN analytics.asset_research_daily ar ON ar.asset_id = bd.asset_id AND ar.dt = bd.dt
  LEFT JOIN analytics.asset_technical_daily at ON at.asset_id = bd.asset_id AND at.dt = bd.dt
  LEFT JOIN event_stock es ON es.asset_id = bd.asset_id AND es.dt = bd.dt
),
scored AS (
  SELECT
    *,
    (ln(1 + report_count) * 1.5 + institution_count * 0.4 + positive_rating_count * 0.8 + upgrade_count * 2.0 + first_coverage_count * 1.2)::double precision AS research_score,
    rating_score_avg::double precision AS rating_score,
    (
      CASE WHEN breakout_20d THEN 2.0 ELSE 0 END
      + CASE WHEN new_high_20d THEN 1.0 ELSE 0 END
      + LEAST(GREATEST(ret_5d * 10, -2), 2)
      + LEAST(GREATEST((volume_ratio_20d - 1), -1), 2)
    )::double precision AS technical_score,
    event_score::double precision AS news_score
  FROM joined
)
INSERT INTO analytics.asset_signal_daily (
  asset_id, dt, stock_code, stock_name, research_score, rating_score,
  technical_score, news_score, policy_score, composite_score, signal_tags,
  evidence, updated_at
)
SELECT
  asset_id,
  dt,
  stock_code,
  stock_name,
  research_score,
  rating_score,
  technical_score,
  news_score,
  policy_score,
  (research_score + rating_score * 0.6 + technical_score + news_score + policy_score)::double precision AS composite_score,
  array_remove(ARRAY[
    CASE WHEN research_score >= 3 THEN 'research_momentum' END,
    CASE WHEN rating_score >= 4 THEN 'positive_rating' END,
    CASE WHEN technical_score >= 2 THEN 'technical_breakout' END,
    CASE WHEN news_score > 0 THEN 'positive_news' END,
    CASE WHEN policy_score > 0 THEN 'policy_support' END
  ]::text[] || technical_tags || event_types, NULL),
  jsonb_build_object(
    'report_count', report_count,
    'institution_count', institution_count,
    'positive_rating_count', positive_rating_count,
    'upgrade_count', upgrade_count,
    'first_coverage_count', first_coverage_count,
    'ret_5d', ret_5d,
    'ret_20d', ret_20d,
    'volume_ratio_20d', volume_ratio_20d,
    'research_documents', research_documents
  ),
  now()
FROM scored
ON CONFLICT (asset_id, dt) DO UPDATE SET
  stock_code = EXCLUDED.stock_code,
  stock_name = EXCLUDED.stock_name,
  research_score = EXCLUDED.research_score,
  rating_score = EXCLUDED.rating_score,
  technical_score = EXCLUDED.technical_score,
  news_score = EXCLUDED.news_score,
  policy_score = EXCLUDED.policy_score,
  composite_score = EXCLUDED.composite_score,
  signal_tags = EXCLUDED.signal_tags,
  evidence = EXCLUDED.evidence,
  updated_at = now()
"""

    alert_sql = """
INSERT INTO analytics.signal_alerts (
  signal_date, signal_type, target_type, target_key, target_name, score, title, summary, evidence, updated_at
)
SELECT
  dt,
  'asset_composite',
  'stock',
  stock_code,
  stock_name,
  composite_score,
  concat(stock_code, ' ', COALESCE(stock_name, ''), ' composite signal ', round(composite_score::numeric, 2)),
  concat_ws('; ',
    CASE WHEN 'research_momentum' = ANY(signal_tags) THEN '研报热度增强' END,
    CASE WHEN 'positive_rating' = ANY(signal_tags) THEN '评级偏积极' END,
    CASE WHEN 'technical_breakout' = ANY(signal_tags) THEN '走势突破或量能增强' END,
    CASE WHEN 'policy_support' = ANY(signal_tags) THEN '政策/投资类文本事件偏积极' END
  ),
  jsonb_build_object('asset_id', asset_id, 'signal_tags', signal_tags, 'evidence', evidence),
  now()
FROM analytics.asset_signal_daily
WHERE composite_score >= %(threshold)s
  AND stock_code IS NOT NULL
  AND (%(since)s::date IS NULL OR dt >= %(since)s::date)
  AND (%(until)s::date IS NULL OR dt <= %(until)s::date)
ON CONFLICT (signal_date, signal_type, target_type, target_key) DO UPDATE SET
  target_name = EXCLUDED.target_name,
  score = EXCLUDED.score,
  title = EXCLUDED.title,
  summary = EXCLUDED.summary,
  evidence = EXCLUDED.evidence,
  updated_at = now()
"""

    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(signal_sql, params)
            print(f"asset_signal_daily: upserted {cur.rowcount}")
            cur.execute(alert_sql, params)
            print(f"signal_alerts: upserted {cur.rowcount}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
