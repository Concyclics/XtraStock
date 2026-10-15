# -*- coding: utf-8 -*-
"""Build final AI hardware company-theme scores from the knowledge graph."""
from __future__ import annotations

import argparse

from common import connect


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", help="Analysis date. Defaults to max active exposure date or current_date.")
    parser.add_argument("--lookback-days", type=int, default=90)
    args = parser.parse_args()

    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(%s::date, max(evidence_date), current_date) AS as_of
                FROM kg.company_concept_exposures
                WHERE status = 'active'
                """,
                (args.as_of,),
            )
            as_of = cur.fetchone()["as_of"]
            params = {"as_of": as_of, "lookback": args.lookback_days}
            cur.execute(
                "DELETE FROM analytics.ai_hardware_theme_scores WHERE as_of_date = %(as_of)s",
                params,
            )
            cur.execute(
                """
WITH relation_exposures AS (
  SELECT
    e.concept_id,
    e.stock_code,
    max(e.stock_name) FILTER (WHERE e.stock_name IS NOT NULL) AS stock_name,
    e.relation_type,
    count(*) FILTER (WHERE e.evidence_date >= %(as_of)s - 30)::int AS mention_count_30d,
    count(*) FILTER (WHERE e.evidence_date >= %(as_of)s - %(lookback)s::int)::int AS evidence_count_90d,
    max(e.evidence_date) AS latest_evidence_date,
    array_to_string(array_agg(DISTINCT left(e.evidence, 240)) FILTER (WHERE e.evidence IS NOT NULL), E'\n') AS evidence_text,
    max(e.confidence)::double precision AS confidence
  FROM kg.company_concept_exposures e
  WHERE e.status = 'active'
    AND e.relation_type IN ('direct_product', 'material_supplier', 'equipment_supplier')
    AND e.evidence_date <= %(as_of)s
    AND e.evidence_date >= %(as_of)s - %(lookback)s::int
  GROUP BY e.concept_id, e.stock_code, e.relation_type
),
active_exposures AS (
  SELECT
    concept_id,
    stock_code,
    max(stock_name) FILTER (WHERE stock_name IS NOT NULL) AS stock_name,
    (array_agg(
      relation_type
      ORDER BY CASE relation_type
        WHEN 'direct_product' THEN 3
        WHEN 'equipment_supplier' THEN 2
        WHEN 'material_supplier' THEN 1
        ELSE 0
      END DESC
    ))[1] AS relation_type,
    sum(mention_count_30d)::int AS mention_count_30d,
    sum(evidence_count_90d)::int AS evidence_count_90d,
    max(latest_evidence_date) AS latest_evidence_date,
    regexp_split_to_array(left(string_agg(DISTINCT evidence_text, E'\n'), 2400), E'\n') AS evidence_list,
    max(confidence)::double precision AS confidence
  FROM relation_exposures
  GROUP BY concept_id, stock_code
),
latest_report AS (
  SELECT DISTINCT ON (stock_code)
    stock_code, stock_name, industry_name, report_date, rating, rating_change
  FROM research.reports_resolved
  WHERE report_type = 'stock'
    AND stock_code IS NOT NULL
    AND report_date <= %(as_of)s
  ORDER BY stock_code, report_date DESC, document_id
),
research_counts AS (
  SELECT stock_code, count(*)::int AS research_report_count_180d
  FROM research.reports_resolved
  WHERE report_type = 'stock'
    AND report_date <= %(as_of)s
    AND report_date >= %(as_of)s - 180
  GROUP BY stock_code
),
latest_forecast AS (
  SELECT DISTINCT ON (stock_code)
    stock_code, forecast_year, forecast_eps, next_forecast_year,
    next_forecast_eps, forecast_eps_growth, forward_pe
  FROM analytics.asset_forecast_daily
  WHERE as_of_date <= %(as_of)s
  ORDER BY stock_code, as_of_date DESC
),
latest_technical AS (
  SELECT DISTINCT ON (a.symbol)
    a.symbol AS stock_code,
    t.pattern_tags
  FROM analytics.asset_technical_daily t
  JOIN market.assets a ON a.asset_id = t.asset_id
  WHERE t.dt <= %(as_of)s
  ORDER BY a.symbol, t.dt DESC
),
scored AS (
  SELECT
    ae.concept_id,
    ae.stock_code,
    COALESCE(ae.stock_name, lr.stock_name) AS stock_name,
    ma.industry_name AS asset_industry_name,
    lr.industry_name AS report_industry_name,
    ae.relation_type,
    ae.mention_count_30d,
    ae.evidence_count_90d,
    COALESCE(rc.research_report_count_180d, 0) AS research_report_count_180d,
    lf.forecast_year,
    lf.forecast_eps,
    lf.next_forecast_year,
    lf.next_forecast_eps,
    lf.forecast_eps_growth AS forecast_growth,
    lf.forward_pe,
    lr.rating,
    lr.rating_change,
    COALESCE(lt.pattern_tags, '{}'::text[]) AS technical_tags,
    ae.evidence_list,
    ae.confidence,
    CASE ae.relation_type
      WHEN 'direct_product' THEN 3.0
      WHEN 'equipment_supplier' THEN 2.5
      WHEN 'material_supplier' THEN 2.2
      ELSE 0
    END AS relation_score
  FROM active_exposures ae
  LEFT JOIN market.assets ma ON ma.asset_type = 'stock' AND ma.symbol = ae.stock_code
  LEFT JOIN latest_report lr ON lr.stock_code = ae.stock_code
  LEFT JOIN research_counts rc ON rc.stock_code = ae.stock_code
  LEFT JOIN latest_forecast lf ON lf.stock_code = ae.stock_code
  LEFT JOIN latest_technical lt ON lt.stock_code = ae.stock_code
),
final AS (
  SELECT
    *,
    (
      relation_score
      + ln(1 + mention_count_30d) * 1.3
      + ln(1 + evidence_count_90d) * 0.9
      + research_report_count_180d * 0.08
      + COALESCE(confidence, 0) * 1.5
      + CASE WHEN rating ~ '买入|推荐|增持|跑赢|优于' THEN 0.8 ELSE 0 END
      + CASE WHEN forward_pe IS NOT NULL AND forward_pe > 0 AND forward_pe < 35 THEN 1.0
             WHEN forward_pe IS NOT NULL AND forward_pe >= 35 AND forward_pe <= 80 THEN 0.4
             ELSE 0 END
      + CASE WHEN forecast_growth IS NOT NULL THEN LEAST(GREATEST(forecast_growth, -0.5), 1.5) ELSE 0 END
      + CASE WHEN 'breakout_20d' = ANY(technical_tags) THEN 0.8 ELSE 0 END
      + CASE WHEN 'new_high_20d' = ANY(technical_tags) THEN 0.5 ELSE 0 END
    )::double precision AS score
  FROM scored
  WHERE COALESCE(report_industry_name, asset_industry_name, '') !~ '(证券|银行|保险|多元金融|金融服务|金融控股|信托|期货|资管)'
)
INSERT INTO analytics.ai_hardware_theme_scores (
  as_of_date, concept_id, stock_code, stock_name, relation_type,
  mention_count_30d, evidence_count_90d, research_report_count_180d,
  forecast_year, forecast_eps, next_forecast_year, next_forecast_eps,
  forecast_eps_growth, forward_pe, latest_rating, latest_rating_change,
  technical_tags, score, score_breakdown, evidence, updated_at
)
SELECT
  %(as_of)s, concept_id, stock_code, stock_name, relation_type,
  mention_count_30d, evidence_count_90d, research_report_count_180d,
  forecast_year, forecast_eps, next_forecast_year, next_forecast_eps,
  forecast_growth, forward_pe, rating, rating_change,
  technical_tags, score,
  jsonb_build_object(
    'forecast_semantics','earnings-forecast-v2','eps_unit','CNY/share','pe_price_basis','source_report_date',
    'relation_score', relation_score,
    'confidence', confidence,
    'mention_count_30d', mention_count_30d,
    'evidence_count_90d', evidence_count_90d,
    'research_report_count_180d', research_report_count_180d
  ),
  jsonb_build_object('evidence', evidence_list),
  now()
FROM final
ON CONFLICT (as_of_date, concept_id, stock_code) DO UPDATE SET
  stock_name = EXCLUDED.stock_name,
  relation_type = EXCLUDED.relation_type,
  mention_count_30d = EXCLUDED.mention_count_30d,
  evidence_count_90d = EXCLUDED.evidence_count_90d,
  research_report_count_180d = EXCLUDED.research_report_count_180d,
  forecast_year = EXCLUDED.forecast_year,
  forecast_eps = EXCLUDED.forecast_eps,
  next_forecast_year = EXCLUDED.next_forecast_year,
  next_forecast_eps = EXCLUDED.next_forecast_eps,
  forecast_eps_growth = EXCLUDED.forecast_eps_growth,
  forward_pe = EXCLUDED.forward_pe,
  latest_rating = EXCLUDED.latest_rating,
  latest_rating_change = EXCLUDED.latest_rating_change,
  technical_tags = EXCLUDED.technical_tags,
  score = EXCLUDED.score,
  score_breakdown = EXCLUDED.score_breakdown,
  evidence = EXCLUDED.evidence,
  updated_at = now()
                """,
                params,
            )
            print(f"ai_hardware_theme_scores: upserted {cur.rowcount} as_of={as_of}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
