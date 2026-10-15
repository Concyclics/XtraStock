# -*- coding: utf-8 -*-
"""Build AI hardware company exposure and fine-grained subindustry trend tables."""
from __future__ import annotations

import argparse

from common import connect


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", help="Analysis date. Defaults to latest mention date.")
    parser.add_argument("--lookback-days", type=int, default=90)
    args = parser.parse_args()

    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(%s::date, max(mention_date), current_date) AS as_of FROM analytics.ai_hardware_mentions",
                (args.as_of,),
            )
            as_of = cur.fetchone()["as_of"]

            cur.execute(
                """
WITH latest_report AS (
  SELECT DISTINCT ON (stock_code)
    stock_code, stock_name, report_date, rating, rating_change, document_id
  FROM research.reports_resolved
  WHERE report_type = 'stock'
    AND stock_code IS NOT NULL
    AND report_date <= %(as_of)s
    AND report_date >= %(as_of)s - 180
  ORDER BY stock_code, report_date DESC, document_id
),
forecast_current AS (
  SELECT stock_code,forecast_year,forecast_eps AS eps,forward_pe AS pe
  FROM analytics.asset_forecast_daily WHERE as_of_date=%(as_of)s
),
forecast_next AS (
  SELECT stock_code,next_forecast_year AS forecast_year,next_forecast_eps AS eps
  FROM analytics.asset_forecast_daily WHERE as_of_date=%(as_of)s
),
mentions AS (
  SELECT
    sc.stock_code,
    m.term_id,
    count(*) FILTER (WHERE m.mention_date >= %(as_of)s - 30)::int AS mention_count_30d,
    count(*) FILTER (WHERE m.mention_date >= %(as_of)s - %(lookback)s::int)::int AS mention_count_90d,
    max(m.mention_date) AS latest_mention_date,
    array_agg(DISTINCT left(m.evidence, 240)) FILTER (WHERE m.evidence IS NOT NULL) AS evidence_list
  FROM analytics.ai_hardware_mentions m
  JOIN LATERAL unnest(m.stock_codes) AS sc(stock_code) ON true
  WHERE m.mention_date <= %(as_of)s
    AND m.mention_date >= %(as_of)s - %(lookback)s::int
  GROUP BY sc.stock_code, m.term_id
),
research_counts AS (
  SELECT
    r.stock_code,
    count(*)::int AS research_report_count_180d
  FROM research.reports_resolved r
  WHERE r.report_type = 'stock'
    AND r.report_date <= %(as_of)s
    AND r.report_date >= %(as_of)s - 180
  GROUP BY r.stock_code
),
asset_lookup AS (
  SELECT symbol AS stock_code, min(asset_id::text)::uuid AS asset_id, max(name) AS stock_name
  FROM market.assets
  WHERE asset_type = 'stock'
  GROUP BY symbol
)
INSERT INTO analytics.ai_hardware_asset_exposure (
  asset_id, stock_code, stock_name, term_id, subindustry, as_of_date,
  mention_count_30d, mention_count_90d, research_report_count_180d,
  latest_report_date, latest_rating, latest_rating_change,
  forecast_year, forecast_eps, previous_forecast_year, previous_forecast_eps,
  forecast_eps_growth, forward_pe, exposure_score, evidence, updated_at
)
SELECT
  al.asset_id,
  m.stock_code,
  COALESCE(lr.stock_name, al.stock_name),
  m.term_id,
  t.subindustry,
  %(as_of)s,
  m.mention_count_30d,
  m.mention_count_90d,
  COALESCE(rc.research_report_count_180d, 0),
  lr.report_date,
  lr.rating,
  lr.rating_change,
  fc.forecast_year,
  fc.eps,
  fp.forecast_year,
  fp.eps,
  CASE
    WHEN fc.eps IS NOT NULL AND fc.eps > 0 AND fp.eps IS NOT NULL
    THEN (fp.eps::double precision / fc.eps::double precision - 1)
  END AS forecast_eps_growth,
  fc.pe,
  (
    ln(1 + m.mention_count_30d) * 2.0
    + ln(1 + m.mention_count_90d)
    + COALESCE(rc.research_report_count_180d, 0) * 0.15
    + t.ai_hardware_relevance * 2.0
    + CASE WHEN lr.rating ~ '买入|推荐|增持|跑赢|优于' THEN 1.0 ELSE 0 END
    + CASE WHEN fc.pe IS NOT NULL AND fc.pe > 0 AND fc.pe < 35 THEN 0.8 ELSE 0 END
    + CASE WHEN fc.eps IS NOT NULL AND fc.eps > 0 AND fp.eps IS NOT NULL AND fp.eps > fc.eps THEN 0.8 ELSE 0 END
  )::double precision AS exposure_score,
  jsonb_build_object(
    'forecast_semantics','earnings-forecast-v2', 'eps_unit','CNY/share',
    'latest_mention_date', m.latest_mention_date,
    'evidence', m.evidence_list,
    'term', t.term,
    'normalized_term', t.normalized_term,
    'relevance_reason', t.relevance_reason
  ),
  now()
FROM mentions m
JOIN analytics.ai_hardware_terms t ON t.term_id = m.term_id
LEFT JOIN asset_lookup al ON al.stock_code = m.stock_code
LEFT JOIN latest_report lr ON lr.stock_code = m.stock_code
LEFT JOIN research_counts rc ON rc.stock_code = m.stock_code
LEFT JOIN forecast_current fc ON fc.stock_code = m.stock_code
LEFT JOIN forecast_next fp ON fp.stock_code = m.stock_code
WHERE m.stock_code IS NOT NULL AND m.stock_code <> ''
ON CONFLICT (stock_code, term_id, as_of_date) DO UPDATE SET
  asset_id = EXCLUDED.asset_id,
  stock_name = EXCLUDED.stock_name,
  subindustry = EXCLUDED.subindustry,
  mention_count_30d = EXCLUDED.mention_count_30d,
  mention_count_90d = EXCLUDED.mention_count_90d,
  research_report_count_180d = EXCLUDED.research_report_count_180d,
  latest_report_date = EXCLUDED.latest_report_date,
  latest_rating = EXCLUDED.latest_rating,
  latest_rating_change = EXCLUDED.latest_rating_change,
  forecast_year = EXCLUDED.forecast_year,
  forecast_eps = EXCLUDED.forecast_eps,
  previous_forecast_year = EXCLUDED.previous_forecast_year,
  previous_forecast_eps = EXCLUDED.previous_forecast_eps,
  forecast_eps_growth = EXCLUDED.forecast_eps_growth,
  forward_pe = EXCLUDED.forward_pe,
  exposure_score = EXCLUDED.exposure_score,
  evidence = EXCLUDED.evidence,
  updated_at = now()
                """,
                {"as_of": as_of, "lookback": args.lookback_days},
            )
            print(f"ai_hardware_asset_exposure: upserted {cur.rowcount} as_of={as_of}")

            cur.execute(
                """
WITH mention_day AS (
  SELECT
    t.subindustry,
    m.mention_date AS dt,
    count(*)::int AS mention_count,
    count(DISTINCT m.document_id)::int AS distinct_document_count,
    count(DISTINCT sc.stock_code)::int AS distinct_stock_count,
    array_agg(DISTINCT t.term ORDER BY t.term) AS terms
  FROM analytics.ai_hardware_mentions m
  JOIN analytics.ai_hardware_terms t ON t.term_id = m.term_id
  LEFT JOIN LATERAL unnest(m.stock_codes) AS sc(stock_code) ON true
  WHERE m.mention_date <= %(as_of)s
    AND m.mention_date >= %(as_of)s - %(lookback)s::int
  GROUP BY t.subindustry, m.mention_date
),
event_day AS (
  SELECT
    i AS subindustry,
    e.event_date AS dt,
    sum(CASE WHEN e.polarity > 0 THEN COALESCE(e.impact_score, 0.5) ELSE 0 END)::double precision AS positive_event_score,
    sum(CASE WHEN e.polarity < 0 THEN abs(COALESCE(e.impact_score, 0.5)) ELSE 0 END)::double precision AS negative_event_score
  FROM analytics.document_events e
  JOIN LATERAL unnest(e.industries) AS i ON true
  WHERE e.event_date <= %(as_of)s
    AND e.event_date >= %(as_of)s - %(lookback)s::int
  GROUP BY i, e.event_date
),
research_day AS (
  SELECT
    industry_name AS subindustry,
    dt,
    report_count,
    positive_rating_count
  FROM analytics.industry_research_daily
  WHERE dt <= %(as_of)s
    AND dt >= %(as_of)s - %(lookback)s::int
),
exposure_day AS (
  SELECT
    subindustry,
    as_of_date AS dt,
    avg(forward_pe) FILTER (WHERE forward_pe IS NOT NULL AND forward_pe > 0) AS avg_forward_pe,
    avg(forecast_eps_growth) FILTER (WHERE forecast_eps_growth IS NOT NULL) AS avg_growth,
    jsonb_agg(
      jsonb_build_object(
        'stock_code', stock_code,
        'stock_name', stock_name,
        'term', (SELECT term FROM analytics.ai_hardware_terms t WHERE t.term_id = e.term_id),
        'score', round(exposure_score::numeric, 2),
        'forward_pe', forward_pe,
        'forecast_growth', forecast_eps_growth
      )
      ORDER BY exposure_score DESC
    ) FILTER (WHERE exposure_score IS NOT NULL) AS top_stocks
  FROM analytics.ai_hardware_asset_exposure e
  WHERE as_of_date = %(as_of)s
  GROUP BY subindustry, as_of_date
)
INSERT INTO analytics.ai_hardware_subindustry_daily (
  subindustry, dt, mention_count, distinct_document_count, distinct_stock_count,
  positive_event_score, negative_event_score, research_report_count, positive_rating_count,
  avg_forward_pe, avg_forecast_eps_growth, top_terms, top_stocks, evidence, updated_at
)
SELECT
  md.subindustry,
  md.dt,
  md.mention_count,
  md.distinct_document_count,
  md.distinct_stock_count,
  COALESCE(ed.positive_event_score, 0),
  COALESCE(ed.negative_event_score, 0),
  COALESCE(rd.report_count, 0),
  COALESCE(rd.positive_rating_count, 0),
  ex.avg_forward_pe,
  ex.avg_growth,
  md.terms,
  COALESCE((
    SELECT jsonb_agg(x)
    FROM jsonb_array_elements(COALESCE(ex.top_stocks, '[]'::jsonb)) WITH ORDINALITY AS arr(x, rn)
    WHERE rn <= 10
  ), '[]'::jsonb),
  jsonb_build_object('as_of', %(as_of)s),
  now()
FROM mention_day md
LEFT JOIN event_day ed ON ed.subindustry = md.subindustry AND ed.dt = md.dt
LEFT JOIN research_day rd ON rd.subindustry = md.subindustry AND rd.dt = md.dt
LEFT JOIN exposure_day ex ON ex.subindustry = md.subindustry AND ex.dt = %(as_of)s
ON CONFLICT (subindustry, dt) DO UPDATE SET
  mention_count = EXCLUDED.mention_count,
  distinct_document_count = EXCLUDED.distinct_document_count,
  distinct_stock_count = EXCLUDED.distinct_stock_count,
  positive_event_score = EXCLUDED.positive_event_score,
  negative_event_score = EXCLUDED.negative_event_score,
  research_report_count = EXCLUDED.research_report_count,
  positive_rating_count = EXCLUDED.positive_rating_count,
  avg_forward_pe = EXCLUDED.avg_forward_pe,
  avg_forecast_eps_growth = EXCLUDED.avg_forecast_eps_growth,
  top_terms = EXCLUDED.top_terms,
  top_stocks = EXCLUDED.top_stocks,
  evidence = EXCLUDED.evidence,
  updated_at = now()
                """,
                {"as_of": as_of, "lookback": args.lookback_days},
            )
            print(f"ai_hardware_subindustry_daily: upserted {cur.rowcount} as_of={as_of}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
