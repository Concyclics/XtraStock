#!/usr/bin/env python3
from __future__ import annotations

import argparse

from common import connect


def build_sql() -> str:
    return """
WITH latest_factors AS (
  SELECT DISTINCT ON (f.asset_id, f.factor_name)
    f.asset_id, f.factor_name, f.factor_value, f.ts
  FROM market.factors_daily f
  WHERE f.factor_name = ANY(ARRAY['pe_ttm', 'pb'])
    AND f.ts <= %(as_of)s::date
    AND coalesce(f.available_at, f.created_at) <=
        ((%(as_of)s::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
  ORDER BY f.asset_id, f.factor_name, f.ts DESC, f.created_at DESC
), valuation AS (
  SELECT asset_id,
         max(factor_value) FILTER (WHERE factor_name = 'pe_ttm') / 100.0 AS pe_ttm,
         max(factor_value) FILTER (WHERE factor_name = 'pb') / 100.0 AS pb,
         max(ts) AS valuation_date
  FROM latest_factors
  GROUP BY asset_id
), technical AS (
  SELECT asset_id, dt, close, pct_chg_1d, ret_5d, ret_20d,
         volatility_20d, drawdown_20d, pattern_tags
  FROM analytics.asset_technical_daily
  WHERE dt = (SELECT max(dt) FROM analytics.asset_technical_daily WHERE dt <= %(as_of)s::date)
), research AS (
  SELECT stock_code, dt, report_count, positive_rating_count, upgrade_count
  FROM analytics.asset_research_daily
  WHERE dt = (SELECT max(dt) FROM analytics.asset_research_daily WHERE dt <= %(as_of)s::date)
), forecast AS (
  SELECT stock_code, as_of_date, forward_pe, next_forward_pe,
         forecast_eps_growth, consensus_eps,
         consensus_eps_median, consensus_eps_stddev,
         forecast_institution_count_180d, latest_rating, latest_rating_change
  FROM analytics.asset_forecast_daily
  WHERE as_of_date = %(as_of)s::date
), signal AS (
  SELECT asset_id, dt, composite_score
  FROM analytics.asset_signal_daily
  WHERE dt = (SELECT max(dt) FROM analytics.asset_signal_daily WHERE dt <= %(as_of)s::date)
), latest_financial AS (
  SELECT DISTINCT ON (f.asset_id, f.metric_name)
    f.asset_id, f.metric_name, f.report_date, f.report_period, f.metric_yoy, f.confidence
  FROM company.financial_statement_metrics f
  WHERE f.metric_name = ANY(ARRAY['revenue', 'net_profit_parent', 'deducted_net_profit'])
    AND f.report_date <= %(as_of)s::date
    AND coalesce(f.available_at, f.created_at) <=
        ((%(as_of)s::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
    -- Only company-total metrics are safe for a cross-stock screening field.
    -- Legacy regex rows can describe a segment, product, or channel instead.
    AND coalesce(f.metadata ->> 'scope', '') = 'company_total'
  ORDER BY f.asset_id, f.metric_name, f.report_date DESC, f.updated_at DESC, f.id DESC
), fundamentals AS (
  SELECT
    asset_id,
    max(report_date) AS actual_report_date,
    (array_agg(report_period ORDER BY report_date DESC))[1] AS actual_report_period,
    max(metric_yoy) FILTER (WHERE metric_name = 'revenue') AS actual_revenue_yoy,
    max(metric_yoy) FILTER (WHERE metric_name = 'net_profit_parent') AS actual_net_profit_yoy,
    max(metric_yoy) FILTER (WHERE metric_name = 'deducted_net_profit') AS actual_deducted_net_profit_yoy,
    max(confidence) AS actual_financial_confidence
  FROM latest_financial
  GROUP BY asset_id
), source_rows AS (
  SELECT
    a.asset_id, a.symbol AS stock_code, a.name AS stock_name,
    t.dt AS technical_date, r.dt AS research_date,
    f.as_of_date AS forecast_date, s.dt AS signal_date,
    t.close, t.pct_chg_1d, t.ret_5d, t.ret_20d,
    t.volatility_20d, t.drawdown_20d,
    coalesce(t.pattern_tags, ARRAY[]::text[]) AS pattern_tags,
    v.pe_ttm, v.pb, f.forward_pe, f.next_forward_pe,
    f.forecast_eps_growth, f.consensus_eps,
    f.consensus_eps_median, f.consensus_eps_stddev,
    f.forecast_institution_count_180d, f.latest_rating, f.latest_rating_change,
    r.report_count AS recent_report_count, r.positive_rating_count, r.upgrade_count,
    s.composite_score,
    fi.actual_report_date, fi.actual_report_period,
    fi.actual_revenue_yoy, fi.actual_net_profit_yoy, fi.actual_deducted_net_profit_yoy,
    fi.actual_financial_confidence,
    jsonb_strip_nulls(jsonb_build_object(
      'technical_date', t.dt, 'research_date', r.dt,
      'forecast_date', f.as_of_date, 'signal_date', s.dt,
      'valuation_date', v.valuation_date, 'actual_report_date', fi.actual_report_date
    )) AS source_dates
  FROM market.assets a
  LEFT JOIN technical t ON t.asset_id = a.asset_id
  LEFT JOIN valuation v ON v.asset_id = a.asset_id
  LEFT JOIN research r ON r.stock_code = a.symbol
  LEFT JOIN forecast f ON f.stock_code = a.symbol
  LEFT JOIN signal s ON s.asset_id = a.asset_id
  LEFT JOIN fundamentals fi ON fi.asset_id = a.asset_id
  WHERE a.asset_type = 'stock'
    AND (t.asset_id IS NOT NULL OR r.stock_code IS NOT NULL OR f.stock_code IS NOT NULL OR s.asset_id IS NOT NULL)
)
INSERT INTO analytics.asset_snapshot_daily (
  asset_id, stock_code, stock_name, as_of_date,
  technical_date, research_date, forecast_date, signal_date,
  close, pct_chg_1d, ret_5d, ret_20d, volatility_20d, drawdown_20d, pattern_tags,
  pe_ttm, pb, forward_pe, next_forward_pe, forecast_eps_growth,
  consensus_eps, consensus_eps_median, consensus_eps_stddev,
  forecast_institution_count_180d, latest_rating, latest_rating_change,
  recent_report_count, positive_rating_count, upgrade_count, composite_score,
  actual_report_date, actual_report_period, actual_revenue_yoy, actual_net_profit_yoy,
  actual_deducted_net_profit_yoy, actual_financial_confidence,
  source_dates, updated_at
)
SELECT
  asset_id, stock_code, stock_name, %(as_of)s::date,
  technical_date, research_date, forecast_date, signal_date,
  close, pct_chg_1d, ret_5d, ret_20d, volatility_20d, drawdown_20d, pattern_tags,
  pe_ttm, pb, forward_pe, next_forward_pe, forecast_eps_growth,
  consensus_eps, consensus_eps_median, consensus_eps_stddev,
  forecast_institution_count_180d, latest_rating, latest_rating_change,
  recent_report_count, positive_rating_count, upgrade_count, composite_score,
  actual_report_date, actual_report_period, actual_revenue_yoy, actual_net_profit_yoy,
  actual_deducted_net_profit_yoy, actual_financial_confidence,
  source_dates, now()
FROM source_rows
ON CONFLICT (asset_id, as_of_date) DO UPDATE SET
  stock_code = EXCLUDED.stock_code,
  stock_name = EXCLUDED.stock_name,
  technical_date = EXCLUDED.technical_date,
  research_date = EXCLUDED.research_date,
  forecast_date = EXCLUDED.forecast_date,
  signal_date = EXCLUDED.signal_date,
  close = EXCLUDED.close,
  pct_chg_1d = EXCLUDED.pct_chg_1d,
  ret_5d = EXCLUDED.ret_5d,
  ret_20d = EXCLUDED.ret_20d,
  volatility_20d = EXCLUDED.volatility_20d,
  drawdown_20d = EXCLUDED.drawdown_20d,
  pattern_tags = EXCLUDED.pattern_tags,
  pe_ttm = EXCLUDED.pe_ttm,
  pb = EXCLUDED.pb,
  forward_pe = EXCLUDED.forward_pe,
  next_forward_pe = EXCLUDED.next_forward_pe,
  forecast_eps_growth = EXCLUDED.forecast_eps_growth,
  consensus_eps = EXCLUDED.consensus_eps,
  consensus_eps_median = EXCLUDED.consensus_eps_median,
  consensus_eps_stddev = EXCLUDED.consensus_eps_stddev,
  forecast_institution_count_180d = EXCLUDED.forecast_institution_count_180d,
  latest_rating = EXCLUDED.latest_rating,
  latest_rating_change = EXCLUDED.latest_rating_change,
  recent_report_count = EXCLUDED.recent_report_count,
  positive_rating_count = EXCLUDED.positive_rating_count,
  upgrade_count = EXCLUDED.upgrade_count,
  composite_score = EXCLUDED.composite_score,
  actual_report_date = EXCLUDED.actual_report_date,
  actual_report_period = EXCLUDED.actual_report_period,
  actual_revenue_yoy = EXCLUDED.actual_revenue_yoy,
  actual_net_profit_yoy = EXCLUDED.actual_net_profit_yoy,
  actual_deducted_net_profit_yoy = EXCLUDED.actual_deducted_net_profit_yoy,
  actual_financial_confidence = EXCLUDED.actual_financial_confidence,
  source_dates = EXCLUDED.source_dates,
  updated_at = now()
WHERE ROW(
  analytics.asset_snapshot_daily.stock_code,
  analytics.asset_snapshot_daily.stock_name,
  analytics.asset_snapshot_daily.technical_date,
  analytics.asset_snapshot_daily.research_date,
  analytics.asset_snapshot_daily.forecast_date,
  analytics.asset_snapshot_daily.signal_date,
  analytics.asset_snapshot_daily.close,
  analytics.asset_snapshot_daily.pct_chg_1d,
  analytics.asset_snapshot_daily.ret_5d,
  analytics.asset_snapshot_daily.ret_20d,
  analytics.asset_snapshot_daily.volatility_20d,
  analytics.asset_snapshot_daily.drawdown_20d,
  analytics.asset_snapshot_daily.pattern_tags,
  analytics.asset_snapshot_daily.pe_ttm,
  analytics.asset_snapshot_daily.pb,
  analytics.asset_snapshot_daily.forward_pe,
  analytics.asset_snapshot_daily.next_forward_pe,
  analytics.asset_snapshot_daily.forecast_eps_growth,
  analytics.asset_snapshot_daily.consensus_eps,
  analytics.asset_snapshot_daily.consensus_eps_median,
  analytics.asset_snapshot_daily.consensus_eps_stddev,
  analytics.asset_snapshot_daily.forecast_institution_count_180d,
  analytics.asset_snapshot_daily.latest_rating,
  analytics.asset_snapshot_daily.latest_rating_change,
  analytics.asset_snapshot_daily.recent_report_count,
  analytics.asset_snapshot_daily.positive_rating_count,
  analytics.asset_snapshot_daily.upgrade_count,
  analytics.asset_snapshot_daily.composite_score,
  analytics.asset_snapshot_daily.actual_report_date,
  analytics.asset_snapshot_daily.actual_report_period,
  analytics.asset_snapshot_daily.actual_revenue_yoy,
  analytics.asset_snapshot_daily.actual_net_profit_yoy,
  analytics.asset_snapshot_daily.actual_deducted_net_profit_yoy,
  analytics.asset_snapshot_daily.actual_financial_confidence,
  analytics.asset_snapshot_daily.source_dates
) IS DISTINCT FROM ROW(
  EXCLUDED.stock_code,
  EXCLUDED.stock_name,
  EXCLUDED.technical_date,
  EXCLUDED.research_date,
  EXCLUDED.forecast_date,
  EXCLUDED.signal_date,
  EXCLUDED.close,
  EXCLUDED.pct_chg_1d,
  EXCLUDED.ret_5d,
  EXCLUDED.ret_20d,
  EXCLUDED.volatility_20d,
  EXCLUDED.drawdown_20d,
  EXCLUDED.pattern_tags,
  EXCLUDED.pe_ttm,
  EXCLUDED.pb,
  EXCLUDED.forward_pe,
  EXCLUDED.next_forward_pe,
  EXCLUDED.forecast_eps_growth,
  EXCLUDED.consensus_eps,
  EXCLUDED.consensus_eps_median,
  EXCLUDED.consensus_eps_stddev,
  EXCLUDED.forecast_institution_count_180d,
  EXCLUDED.latest_rating,
  EXCLUDED.latest_rating_change,
  EXCLUDED.recent_report_count,
  EXCLUDED.positive_rating_count,
  EXCLUDED.upgrade_count,
  EXCLUDED.composite_score,
  EXCLUDED.actual_report_date,
  EXCLUDED.actual_report_period,
  EXCLUDED.actual_revenue_yoy,
  EXCLUDED.actual_net_profit_yoy,
  EXCLUDED.actual_deducted_net_profit_yoy,
  EXCLUDED.actual_financial_confidence,
  EXCLUDED.source_dates
)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build point-in-time asset analysis snapshots.")
    parser.add_argument("--as-of", required=True, help="Snapshot date YYYY-MM-DD.")
    args = parser.parse_args()
    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(build_sql(), {"as_of": args.as_of})
            print(f"asset_snapshot_daily: upserted {cur.rowcount} as_of={args.as_of}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
