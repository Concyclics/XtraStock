# -*- coding: utf-8 -*-
"""Build daily technical features from the V2 point-in-time total-return series."""
from __future__ import annotations

import argparse

from common import connect


def build_sql(limit_assets: int | None, stock_code: str | None = None) -> str:
    limit_clause = f"LIMIT {int(limit_assets)}" if limit_assets else ""
    stock_clause = "AND a.symbol = %(stock_code)s" if stock_code else ""
    return f"""
WITH calendar AS (
  SELECT trade_date, row_number() OVER (ORDER BY trade_date) AS session_no
  FROM market.trading_calendar
  WHERE exchange='SSE' AND session_status='open'
    AND (is_verified OR source_name='observed market bars fallback')
), asset_filter AS (
  SELECT a.asset_id
  FROM market.assets a
  WHERE a.asset_type = ANY(%(asset_types)s)
    AND NOT (a.metadata ? 'identity_alias_of')
    {stock_clause}
    AND EXISTS (
      SELECT 1
      FROM market.canonical_total_return_daily_v2 b
      WHERE b.asset_id=a.asset_id
        AND (%(since)s::date IS NULL OR b.dt >= %(since)s::date - 150)
        AND (%(until)s::date IS NULL OR b.dt <= %(until)s::date)
      LIMIT 1
    )
  ORDER BY a.asset_type, a.symbol
  {limit_clause}
),
daily_raw AS (
  SELECT
    b.asset_id,
    b.dt,
    b.open,
    b.high,
    b.low,
    b.close,
    b.volume,
    b.amount,
    b.source || '|canonical_total_return:v2' AS source,
    b.adjustment,
    cal.session_no,
    greatest(b.available_at,b.factor_available_at) AS source_available_at,
    (b.quality_status NOT IN ('conflict','incomplete','rejected')
     AND adj.quality_status='verified' AND b.close>0) AS price_usable,
    adj.quality_status AS adjustment_quality
  FROM market.canonical_total_return_daily_v2 b
  JOIN calendar cal ON cal.trade_date=b.dt
  LEFT JOIN market.adjustment_factors_daily adj ON adj.asset_id=b.asset_id AND adj.dt=b.dt
  JOIN asset_filter af ON af.asset_id=b.asset_id
  WHERE (%(since)s::date IS NULL OR b.dt >= %(since)s::date - 150)
    AND (%(until)s::date IS NULL OR b.dt <= %(until)s::date)
), segment_flags AS (
  SELECT d.*,
         CASE WHEN lag(adjustment) OVER (PARTITION BY asset_id ORDER BY dt)
                        IS DISTINCT FROM adjustment
              THEN 1 ELSE 0 END AS adjustment_changed
  FROM daily_raw d
), segmented AS (
  SELECT f.*,
         sum(adjustment_changed) OVER (
           PARTITION BY asset_id ORDER BY dt ROWS UNBOUNDED PRECEDING
         ) AS adjustment_segment
  FROM segment_flags f
), w1 AS (
  SELECT
    d.*,
    max(source_available_at) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 60 PRECEDING AND CURRENT ROW) AS window_available_at,
    (session_no-lag(session_no,1) OVER asset_win=1
     AND count(*) FILTER (WHERE price_usable IS TRUE) OVER
       (PARTITION BY asset_id,adjustment_segment ORDER BY dt ROWS BETWEEN 1 PRECEDING AND CURRENT ROW)=2) AS contiguous_1,
    (session_no-lag(session_no,2) OVER asset_win=2
     AND count(*) FILTER (WHERE price_usable IS TRUE) OVER
       (PARTITION BY asset_id,adjustment_segment ORDER BY dt ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)=3) AS contiguous_2,
    (session_no-lag(session_no,4) OVER asset_win=4
     AND count(*) FILTER (WHERE price_usable IS TRUE) OVER
       (PARTITION BY asset_id,adjustment_segment ORDER BY dt ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)=5) AS contiguous_4,
    (session_no-lag(session_no,5) OVER asset_win=5
     AND count(*) FILTER (WHERE price_usable IS TRUE) OVER
       (PARTITION BY asset_id,adjustment_segment ORDER BY dt ROWS BETWEEN 5 PRECEDING AND CURRENT ROW)=6) AS contiguous_5,
    (session_no-lag(session_no,9) OVER asset_win=9
     AND count(*) FILTER (WHERE price_usable IS TRUE) OVER
       (PARTITION BY asset_id,adjustment_segment ORDER BY dt ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)=10) AS contiguous_9,
    (session_no-lag(session_no,19) OVER asset_win=19
     AND count(*) FILTER (WHERE price_usable IS TRUE) OVER
       (PARTITION BY asset_id,adjustment_segment ORDER BY dt ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)=20) AS contiguous_19,
    (session_no-lag(session_no,20) OVER asset_win=20
     AND count(*) FILTER (WHERE price_usable IS TRUE) OVER
       (PARTITION BY asset_id,adjustment_segment ORDER BY dt ROWS BETWEEN 20 PRECEDING AND CURRENT ROW)=21) AS contiguous_20,
    (session_no-lag(session_no,59) OVER asset_win=59
     AND count(*) FILTER (WHERE price_usable IS TRUE) OVER
       (PARTITION BY asset_id,adjustment_segment ORDER BY dt ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)=60) AS contiguous_59,
    count(close) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS history_count,
    lag(close, 1) OVER asset_win AS close_1d,
    lag(close, 2) OVER asset_win AS close_2d,
    lag(close, 5) OVER asset_win AS close_5d,
    lag(close, 20) OVER asset_win AS close_20d,
    lag(close, 1) OVER asset_win AS prev_close,
    avg(close) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS ma5,
    avg(close) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) AS ma10,
    avg(close) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20,
    avg(close) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS ma60,
    avg(volume) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg_volume_20_prev,
    avg(amount) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg_amount_20_prev,
    max(close) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS high_close_20,
    min(close) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS low_close_20,
    max(close) OVER (PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS high_close_20_prev
  FROM segmented d
  WINDOW asset_win AS (PARTITION BY asset_id, adjustment_segment ORDER BY dt)
),
w2 AS (
  SELECT
    w1.*,
    CASE WHEN contiguous_1 AND close_1d <> 0 THEN close / close_1d - 1 END AS ret_1d_calc
  FROM w1
),
features AS (
  SELECT
    w2.*,
    stddev_samp(ret_1d_calc) OVER (
      PARTITION BY asset_id, adjustment_segment ORDER BY dt ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
    ) AS volatility_20d
  FROM w2
),
final AS (
  SELECT
    asset_id,
    dt,
    close,
    ret_1d_calc AS pct_chg_1d,
    CASE WHEN contiguous_5 AND close_5d <> 0 THEN close / close_5d - 1 END AS ret_5d,
    CASE WHEN contiguous_20 AND close_20d <> 0 THEN close / close_20d - 1 END AS ret_20d,
    CASE WHEN contiguous_4 THEN ma5 END AS ma5,
    CASE WHEN contiguous_9 THEN ma10 END AS ma10,
    CASE WHEN contiguous_19 THEN ma20 END AS ma20,
    CASE WHEN contiguous_59 THEN ma60 END AS ma60,
    volume,
    amount,
    CASE WHEN contiguous_20 AND avg_volume_20_prev <> 0 THEN volume / avg_volume_20_prev END AS volume_ratio_20d,
    CASE WHEN contiguous_20 AND avg_amount_20_prev <> 0 THEN amount / avg_amount_20_prev END AS amount_ratio_20d,
    CASE WHEN contiguous_20 THEN volatility_20d END AS volatility_20d,
    CASE WHEN contiguous_19 AND high_close_20 <> 0 THEN close / high_close_20 - 1 END AS drawdown_20d,
    contiguous_19 AND close >= high_close_20 AS new_high_20d,
    contiguous_19 AND close <= low_close_20 AS new_low_20d,
    contiguous_2 AND prev_close IS NOT NULL AND prev_close > close_2d AND prev_close > close AS local_peak,
    contiguous_2 AND prev_close IS NOT NULL AND prev_close < close_2d AND prev_close < close AS local_trough,
    contiguous_20 AND high_close_20_prev IS NOT NULL AND close > high_close_20_prev AS breakout_20d,
    source,
    window_available_at AS available_at,
    jsonb_build_object('contract_version','pit-input-v1',
      'calendar_continuous_5',coalesce(contiguous_5,false),
      'calendar_continuous_20',coalesce(contiguous_20,false),
      'calendar_continuous_60',coalesce(contiguous_59,false),
      'adjustment_verified',adjustment_quality='verified',
      'source_available_at',source_available_at,
      'window_available_at',window_available_at,
      'price_quality_policy','verified_adjustment_nonrejected_bar',
      'status',CASE WHEN contiguous_20 THEN 'pass' ELSE 'blocked' END) AS input_quality
  FROM features
  WHERE (%(since)s::date IS NULL OR dt >= %(since)s::date)
)
INSERT INTO analytics.asset_technical_daily (
  asset_id, dt, close, pct_chg_1d, ret_5d, ret_20d, ma5, ma10, ma20, ma60,
  volume, amount, volume_ratio_20d, amount_ratio_20d, volatility_20d, drawdown_20d,
  new_high_20d, new_low_20d, local_peak, local_trough, breakout_20d, pattern_tags,
  source, available_at, input_quality, updated_at
)
SELECT
  asset_id, dt, close, pct_chg_1d, ret_5d, ret_20d, ma5, ma10, ma20, ma60,
  volume, amount, volume_ratio_20d, amount_ratio_20d, volatility_20d, drawdown_20d,
  COALESCE(new_high_20d, false),
  COALESCE(new_low_20d, false),
  COALESCE(local_peak, false),
  COALESCE(local_trough, false),
  COALESCE(breakout_20d, false),
  array_remove(ARRAY[
    CASE WHEN COALESCE(new_high_20d, false) THEN 'new_high_20d' END,
    CASE WHEN COALESCE(new_low_20d, false) THEN 'new_low_20d' END,
    CASE WHEN COALESCE(local_peak, false) THEN 'confirmed_peak_prev_day' END,
    CASE WHEN COALESCE(local_trough, false) THEN 'confirmed_trough_prev_day' END,
    CASE WHEN COALESCE(breakout_20d, false) THEN 'breakout_20d' END,
    CASE WHEN volume_ratio_20d >= 2.0 THEN 'volume_spike' END,
    CASE WHEN pct_chg_1d >= 0.05 THEN 'strong_up_day' END,
    CASE WHEN pct_chg_1d <= -0.05 THEN 'strong_down_day' END,
    CASE WHEN ma5 > ma20 AND ma20 > ma60 THEN 'bullish_ma_stack' END,
    CASE WHEN ma5 < ma20 AND ma20 < ma60 THEN 'bearish_ma_stack' END
  ]::text[], NULL),
  source,
  greatest(available_at,(dt::timestamp + time '15:05') AT TIME ZONE 'Asia/Shanghai'),
  input_quality,
  now()
FROM final
ON CONFLICT (asset_id, dt) DO UPDATE SET
  close = EXCLUDED.close,
  pct_chg_1d = EXCLUDED.pct_chg_1d,
  ret_5d = EXCLUDED.ret_5d,
  ret_20d = EXCLUDED.ret_20d,
  ma5 = EXCLUDED.ma5,
  ma10 = EXCLUDED.ma10,
  ma20 = EXCLUDED.ma20,
  ma60 = EXCLUDED.ma60,
  volume = EXCLUDED.volume,
  amount = EXCLUDED.amount,
  volume_ratio_20d = EXCLUDED.volume_ratio_20d,
  amount_ratio_20d = EXCLUDED.amount_ratio_20d,
  volatility_20d = EXCLUDED.volatility_20d,
  drawdown_20d = EXCLUDED.drawdown_20d,
  new_high_20d = EXCLUDED.new_high_20d,
  new_low_20d = EXCLUDED.new_low_20d,
  local_peak = EXCLUDED.local_peak,
  local_trough = EXCLUDED.local_trough,
  breakout_20d = EXCLUDED.breakout_20d,
  pattern_tags = EXCLUDED.pattern_tags,
  source = EXCLUDED.source,
  available_at = EXCLUDED.available_at,
  input_quality = EXCLUDED.input_quality,
  updated_at = now()
WHERE ROW(
  analytics.asset_technical_daily.close,
  analytics.asset_technical_daily.pct_chg_1d,
  analytics.asset_technical_daily.ret_5d,
  analytics.asset_technical_daily.ret_20d,
  analytics.asset_technical_daily.ma5,
  analytics.asset_technical_daily.ma10,
  analytics.asset_technical_daily.ma20,
  analytics.asset_technical_daily.ma60,
  analytics.asset_technical_daily.volume,
  analytics.asset_technical_daily.amount,
  analytics.asset_technical_daily.volume_ratio_20d,
  analytics.asset_technical_daily.amount_ratio_20d,
  analytics.asset_technical_daily.volatility_20d,
  analytics.asset_technical_daily.drawdown_20d,
  analytics.asset_technical_daily.new_high_20d,
  analytics.asset_technical_daily.new_low_20d,
  analytics.asset_technical_daily.local_peak,
  analytics.asset_technical_daily.local_trough,
  analytics.asset_technical_daily.breakout_20d,
  analytics.asset_technical_daily.pattern_tags,
  analytics.asset_technical_daily.source,
  analytics.asset_technical_daily.available_at,
  analytics.asset_technical_daily.input_quality
) IS DISTINCT FROM ROW(
  EXCLUDED.close,
  EXCLUDED.pct_chg_1d,
  EXCLUDED.ret_5d,
  EXCLUDED.ret_20d,
  EXCLUDED.ma5,
  EXCLUDED.ma10,
  EXCLUDED.ma20,
  EXCLUDED.ma60,
  EXCLUDED.volume,
  EXCLUDED.amount,
  EXCLUDED.volume_ratio_20d,
  EXCLUDED.amount_ratio_20d,
  EXCLUDED.volatility_20d,
  EXCLUDED.drawdown_20d,
  EXCLUDED.new_high_20d,
  EXCLUDED.new_low_20d,
  EXCLUDED.local_peak,
  EXCLUDED.local_trough,
  EXCLUDED.breakout_20d,
  EXCLUDED.pattern_tags,
  EXCLUDED.source,
  EXCLUDED.available_at,
  EXCLUDED.input_quality
)
"""


def cleanup_sql(limit_assets: int | None, stock_code: str | None = None) -> str:
    limit_clause = f"LIMIT {int(limit_assets)}" if limit_assets else ""
    stock_clause = "AND a.symbol = %(stock_code)s" if stock_code else ""
    return f"""
    WITH selected AS (
      SELECT a.asset_id
      FROM market.assets a
      WHERE a.asset_type=ANY(%(asset_types)s)
      {stock_clause}
      ORDER BY a.asset_type, a.symbol
      {limit_clause}
    )
    DELETE FROM analytics.asset_technical_daily t
    USING selected s
    WHERE t.asset_id=s.asset_id
      AND (%(since)s::date IS NULL OR t.dt >= %(since)s::date)
      AND (%(until)s::date IS NULL OR t.dt <= %(until)s::date)
      AND NOT EXISTS (
        SELECT 1 FROM market.canonical_total_return_daily_v2 b
        WHERE b.asset_id=t.asset_id AND b.dt=t.dt
      )
    """


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help="Inclusive trading date, YYYY-MM-DD.")
    parser.add_argument("--until", help="Inclusive trading date, YYYY-MM-DD.")
    parser.add_argument("--asset-types", default="stock", help="Comma-separated market.assets asset_type values.")
    parser.add_argument("--limit-assets", type=int, help="Limit asset count for smoke tests.")
    parser.add_argument("--stock-code", help="Limit a company backfill to this symbol.")
    args = parser.parse_args()

    params = {
        "since": args.since,
        "until": args.until,
        "asset_types": [x.strip() for x in args.asset_types.split(",") if x.strip()],
        "stock_code": args.stock_code,
    }
    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(cleanup_sql(args.limit_assets, args.stock_code), params)
            deleted = cur.rowcount
            cur.execute(build_sql(args.limit_assets, args.stock_code), params)
            print(f"asset_technical_daily: deleted_mixed={deleted} upserted={cur.rowcount}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
