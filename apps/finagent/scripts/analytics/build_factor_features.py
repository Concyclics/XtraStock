#!/usr/bin/env python3
"""Build point-in-time factor features and forward-return labels."""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect


FEATURE_SQL = """
WITH settings AS (
  SELECT coalesce(nullif(current_setting('finagent.knowledge_mode',true),''),'system') AS knowledge_mode,
    coalesce(nullif(current_setting('finagent.decision_time',true),''),'23:59:59')::time AS decision_time
), technical AS (
  SELECT t.*,a.symbol,p.knowledge_mode,
    least(statement_timestamp(),(t.dt::timestamp+p.decision_time) AT TIME ZONE 'Asia/Shanghai') AS cutoff
  FROM analytics.asset_technical_daily t
  JOIN market.assets a USING(asset_id) CROSS JOIN settings p
  WHERE t.dt BETWEEN %(since)s AND %(until)s
    AND a.asset_type='stock' AND NOT (a.metadata ? 'identity_alias_of')
), calendar AS (
  SELECT trade_date,row_number() OVER (ORDER BY trade_date) session_no
  FROM market.trading_calendar WHERE exchange='SSE' AND session_status='open'
    AND (is_verified OR source_name='observed market bars fallback')
), money_latest AS (
  SELECT DISTINCT ON (m.asset_id,m.ts) m.*,c.session_no
  FROM market.money_flow_daily m JOIN calendar c ON c.trade_date=m.ts
  WHERE m.ts BETWEEN %(since)s::date - 15 AND %(until)s
  ORDER BY m.asset_id,m.ts,m.created_at DESC,m.source
), money_base AS (
  SELECT m.asset_id,m.ts,
    CASE WHEN count(main_net) OVER w=5 AND session_no-min(session_no) OVER w=4
         THEN sum(main_net) OVER w END AS main_net_5d,
    max(greatest(created_at,(ts::timestamp+time '15:05') AT TIME ZONE 'Asia/Shanghai')) OVER w AS available_at
  FROM money_latest m
  WINDOW w AS (PARTITION BY m.asset_id ORDER BY m.ts ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
), research_roll AS (
  SELECT t.asset_id,t.dt,count(r.document_id) AS reports_20d,
    count(DISTINCT nullif(trim(r.institution),'')) AS institutions_20d,
    count(*) FILTER (WHERE r.rating_change ~ '调高|上调|提高|升')
      -count(*) FILTER (WHERE r.rating_change ~ '调低|下调|降低|降') AS net_upgrades_20d,
    max(CASE WHEN t.knowledge_mode='system' THEN greatest(r.available_at,r.created_at,d.created_at,d.updated_at,r.identity_known_at)
        ELSE coalesce(r.available_at,d.published_at,r.created_at) END) AS available_at
  FROM technical t
  LEFT JOIN research.reports_resolved r ON r.stock_code=t.symbol AND r.report_type='stock'
    AND r.report_date BETWEEN t.dt-19 AND t.dt
  LEFT JOIN corpus.documents d ON d.id=r.document_id
  WHERE r.document_id IS NULL OR
    (CASE WHEN t.knowledge_mode='system' THEN greatest(r.available_at,r.created_at,d.created_at,d.updated_at,r.identity_known_at)
          ELSE coalesce(r.available_at,d.published_at,r.created_at) END)<=t.cutoff
  GROUP BY t.asset_id,t.dt
), event_base AS (
  SELECT a.asset_id,e.event_date AS dt,
    e.polarity*coalesce(e.impact_score,1.0)*coalesce(e.confidence,.5) AS event_score,
    coalesce(d.published_at,d.created_at,e.created_at) AS public_available_at,
    greatest(d.published_at,d.created_at,d.updated_at,e.created_at) AS system_available_at
  FROM analytics.document_events e LEFT JOIN corpus.documents d ON d.id=e.document_id
  CROSS JOIN LATERAL unnest(e.stock_codes) code(stock_code)
  JOIN market.assets a ON a.asset_type='stock' AND a.symbol=code.stock_code
    AND NOT (a.metadata ? 'identity_alias_of')
  WHERE e.event_date BETWEEN %(since)s::date-5 AND %(until)s
), event_roll AS (
  SELECT t.asset_id,t.dt,coalesce(sum(e.event_score),0) AS event_score_5d,
    max(CASE WHEN t.knowledge_mode='system' THEN e.system_available_at ELSE e.public_available_at END) AS available_at
  FROM technical t LEFT JOIN event_base e ON e.asset_id=t.asset_id AND e.dt BETWEEN t.dt-4 AND t.dt
    AND (CASE WHEN t.knowledge_mode='system' THEN e.system_available_at ELSE e.public_available_at END)<=t.cutoff
  GROUP BY t.asset_id,t.dt
), final AS (
  SELECT t.asset_id, t.dt,
         t.ret_20d AS momentum_20d,
         -t.ret_5d AS reversal_5d,
         -t.volatility_20d AS low_volatility_20d,
         CASE WHEN t.volume_ratio_20d IS NOT NULL THEN t.volume_ratio_20d - 1 END AS volume_surprise_20d,
         CASE WHEN t.amount_ratio_20d IS NOT NULL THEN t.amount_ratio_20d - 1 END AS amount_surprise_20d,
         t.drawdown_20d,
         CASE WHEN t.amount IS NOT NULL AND t.amount <> 0 AND mb.available_at<=t.cutoff
              THEN mb.main_net_5d / nullif(t.amount * 5.0,0) END AS money_flow_intensity_5d,
         ln(1 + rr.reports_20d) + 0.5 * ln(1 + rr.institutions_20d) AS research_breadth_20d,
         rr.net_upgrades_20d / greatest(rr.reports_20d,1)::double precision AS rating_upgrade_20d,
         er.event_score_5d AS event_sentiment_5d,
         gf.direct_exposure_score AS graph_direct_exposure,
         gf.positive_event_score_30d-gf.negative_event_score_30d AS graph_event_impulse_30d,
         ln(1+gf.institution_coverage_30d) AS graph_attention_30d,
         gf.novelty_score_30d AS graph_novelty_30d,
         gf.rating_momentum_60d AS graph_rating_momentum_60d,
         gf.forecast_eps_growth AS graph_forecast_eps_growth,
         gf.forecast_revenue_growth AS graph_forecast_revenue_growth,
         gf.forecast_eps_revision_90d AS graph_forecast_eps_revision_90d,
         gf.forecast_revenue_revision_90d AS graph_forecast_revenue_revision_90d,
         gf.forecast_dispersion_180d AS graph_forecast_dispersion_180d,
         gf.forward_earnings_yield AS graph_forward_earnings_yield,
         gf.target_price_upside AS graph_target_price_upside,
         gf.policy_event_impulse_30d AS graph_policy_impulse_30d,
         gf.demand_event_impulse_30d AS graph_demand_impulse_30d,
         gf.order_event_impulse_30d AS graph_order_impulse_30d,
         gf.pricing_event_impulse_30d AS graph_pricing_impulse_30d,
         gf.risk_event_impulse_30d AS graph_risk_impulse_30d,
         gf.event_source_diversity_30d AS graph_source_diversity_30d,
         gf.graph_two_hop_exposure,
         gf.graph_centrality,
         gf.graph_evidence_diversity,
         gf.graph_relation_stability,
         greatest(t.available_at,rr.available_at,er.available_at,gf.available_at,
           CASE WHEN mb.available_at<=t.cutoff THEN mb.available_at END) AS available_at,
         'pit-input-v1'::text AS input_contract_version,t.cutoff AS decision_cutoff,t.knowledge_mode,
         jsonb_build_object('technical',t.input_quality,
           'source_available_at',jsonb_build_object('technical',t.available_at,'research',rr.available_at,
             'events',er.available_at,'graph',gf.available_at,'money_flow',mb.available_at),
           'graph_contract_version',gf.provenance->>'input_contract_version',
           'graph_knowledge_mode',gf.provenance->>'knowledge_mode',
           'strict_pit_ready',coalesce(t.knowledge_mode='system'
             AND t.input_quality->>'contract_version'='pit-input-v1'
             AND t.input_quality->>'status'='pass' AND t.available_at<=t.cutoff,false),
           'graph_strict_pit_ready',coalesce(gf.provenance->>'input_contract_version'='graph-pit-v1'
             AND gf.provenance->>'knowledge_mode'='system' AND gf.provenance->>'strict_pit_ready'='true' AND gf.available_at<=t.cutoff,false)) AS input_quality,
         jsonb_strip_nulls(jsonb_build_object(
           'technical_date', t.dt, 'money_flow_date', mb.ts,
           'graph_date', gf.dt, 'price_source', t.source,
           'contract_version','pit-input-v1','knowledge_mode',t.knowledge_mode,'decision_cutoff',t.cutoff
         )) AS source_dates
  FROM technical t
  LEFT JOIN money_base mb ON mb.asset_id=t.asset_id AND mb.ts=t.dt
  LEFT JOIN research_roll rr ON rr.asset_id=t.asset_id AND rr.dt=t.dt
  LEFT JOIN event_roll er ON er.asset_id=t.asset_id AND er.dt=t.dt
  LEFT JOIN analytics.graph_feature_daily gf ON gf.asset_id=t.asset_id AND gf.dt=t.dt
    AND gf.available_at<=t.cutoff
    AND gf.provenance->>'knowledge_mode'=t.knowledge_mode
    AND (t.knowledge_mode<>'system' OR (gf.provenance->>'input_contract_version'='graph-pit-v1'
      AND gf.provenance->>'strict_pit_ready'='true'))
)
INSERT INTO factor.feature_daily (
  asset_id, dt, momentum_20d, reversal_5d, low_volatility_20d,
  volume_surprise_20d, amount_surprise_20d, drawdown_20d,
  money_flow_intensity_5d, research_breadth_20d, rating_upgrade_20d,
  event_sentiment_5d, graph_direct_exposure, graph_event_impulse_30d,
  graph_attention_30d, graph_novelty_30d, graph_rating_momentum_60d,
  graph_forecast_eps_growth, graph_forecast_revenue_growth, graph_forecast_eps_revision_90d,
  graph_forecast_revenue_revision_90d, graph_forecast_dispersion_180d,
  graph_forward_earnings_yield, graph_target_price_upside, graph_policy_impulse_30d,
  graph_demand_impulse_30d, graph_order_impulse_30d, graph_pricing_impulse_30d,
  graph_risk_impulse_30d, graph_source_diversity_30d, graph_two_hop_exposure,
  graph_centrality, graph_evidence_diversity, graph_relation_stability,
  available_at, source_dates, input_contract_version, decision_cutoff, knowledge_mode, input_quality, updated_at
)
SELECT asset_id, dt, momentum_20d, reversal_5d, low_volatility_20d,
       volume_surprise_20d, amount_surprise_20d, drawdown_20d,
       money_flow_intensity_5d, research_breadth_20d, rating_upgrade_20d,
       event_sentiment_5d, graph_direct_exposure, graph_event_impulse_30d,
       graph_attention_30d, graph_novelty_30d, graph_rating_momentum_60d,
       graph_forecast_eps_growth, graph_forecast_revenue_growth, graph_forecast_eps_revision_90d,
       graph_forecast_revenue_revision_90d, graph_forecast_dispersion_180d,
       graph_forward_earnings_yield, graph_target_price_upside, graph_policy_impulse_30d,
       graph_demand_impulse_30d, graph_order_impulse_30d, graph_pricing_impulse_30d,
       graph_risk_impulse_30d, graph_source_diversity_30d, graph_two_hop_exposure,
       graph_centrality, graph_evidence_diversity, graph_relation_stability,
       available_at, source_dates, input_contract_version, decision_cutoff, knowledge_mode, input_quality, now()
FROM final
ON CONFLICT (asset_id, dt) DO UPDATE SET
  momentum_20d=EXCLUDED.momentum_20d,
  reversal_5d=EXCLUDED.reversal_5d,
  low_volatility_20d=EXCLUDED.low_volatility_20d,
  volume_surprise_20d=EXCLUDED.volume_surprise_20d,
  amount_surprise_20d=EXCLUDED.amount_surprise_20d,
  drawdown_20d=EXCLUDED.drawdown_20d,
  money_flow_intensity_5d=EXCLUDED.money_flow_intensity_5d,
  research_breadth_20d=EXCLUDED.research_breadth_20d,
  rating_upgrade_20d=EXCLUDED.rating_upgrade_20d,
  event_sentiment_5d=EXCLUDED.event_sentiment_5d,
  graph_direct_exposure=EXCLUDED.graph_direct_exposure,
  graph_event_impulse_30d=EXCLUDED.graph_event_impulse_30d,
  graph_attention_30d=EXCLUDED.graph_attention_30d,
  graph_novelty_30d=EXCLUDED.graph_novelty_30d,
  graph_rating_momentum_60d=EXCLUDED.graph_rating_momentum_60d,
  graph_forecast_eps_growth=EXCLUDED.graph_forecast_eps_growth,
  graph_forecast_revenue_growth=EXCLUDED.graph_forecast_revenue_growth,
  graph_forecast_eps_revision_90d=EXCLUDED.graph_forecast_eps_revision_90d,
  graph_forecast_revenue_revision_90d=EXCLUDED.graph_forecast_revenue_revision_90d,
  graph_forecast_dispersion_180d=EXCLUDED.graph_forecast_dispersion_180d,
  graph_forward_earnings_yield=EXCLUDED.graph_forward_earnings_yield,
  graph_target_price_upside=EXCLUDED.graph_target_price_upside,
  graph_policy_impulse_30d=EXCLUDED.graph_policy_impulse_30d,
  graph_demand_impulse_30d=EXCLUDED.graph_demand_impulse_30d,
  graph_order_impulse_30d=EXCLUDED.graph_order_impulse_30d,
  graph_pricing_impulse_30d=EXCLUDED.graph_pricing_impulse_30d,
  graph_risk_impulse_30d=EXCLUDED.graph_risk_impulse_30d,
  graph_source_diversity_30d=EXCLUDED.graph_source_diversity_30d,
  graph_two_hop_exposure=EXCLUDED.graph_two_hop_exposure,
  graph_centrality=EXCLUDED.graph_centrality,
  graph_evidence_diversity=EXCLUDED.graph_evidence_diversity,
  graph_relation_stability=EXCLUDED.graph_relation_stability,
  available_at=EXCLUDED.available_at,
  source_dates=EXCLUDED.source_dates,
  input_contract_version=EXCLUDED.input_contract_version,
  decision_cutoff=EXCLUDED.decision_cutoff,
  knowledge_mode=EXCLUDED.knowledge_mode,
  input_quality=EXCLUDED.input_quality,
  updated_at=now()
"""


def build_label_sql() -> str:
    """Exact-session labels; missing/invalid observations never shorten a horizon."""
    horizons=(1,5,20,60)
    lead_columns=[]
    for h in horizons:
        lead_columns.extend([
            f"lead(b.dt,{h}) OVER w AS exit_date_{h}d",
            f"lead(b.close,{h}) OVER w AS close_{h}d",
            f"lead(cal.session_no,{h}) OVER w AS exit_session_{h}d",
            f"count(*) FILTER(WHERE b.quality_status NOT IN ('rejected','conflict','incomplete') AND adj.quality_status='verified') OVER (PARTITION BY b.asset_id ORDER BY b.dt ROWS BETWEEN 1 FOLLOWING AND {h} FOLLOWING) AS valid_prices_{h}d",
            f"max(greatest(b.available_at,b.factor_available_at)) OVER (PARTITION BY b.asset_id ORDER BY b.dt ROWS BETWEEN 1 FOLLOWING AND {h} FOLLOWING) AS source_available_{h}d",
        ])
    valid={h:f"entry_open>0 AND entry_session=session_no+1 AND exit_session_{h}d=session_no+{h} AND valid_prices_{h}d={h}" for h in horizons}
    label_cols=[f"exit_date_{h}d" for h in horizons]+[f"exit_price_{h}d" for h in horizons]+[f"return_{h}d" for h in horizons]+[f"label_available_at_{h}d" for h in horizons]
    values=[f"exit_date_{h}d" for h in horizons]+[f"close_{h}d" for h in horizons]
    values += [f"CASE WHEN {valid[h]}"+(" AND asset_type<>'stock'" if h==1 else "")+f" THEN close_{h}d/entry_open-1 END" for h in horizons]
    values += [f"CASE WHEN {valid[h]} THEN greatest(source_available_{h}d,(exit_date_{h}d::timestamp+time '15:05') AT TIME ZONE 'Asia/Shanghai') END" for h in horizons]
    updates=[f"{col}=EXCLUDED.{col}" for col in ['entry_date','entry_price']+label_cols+['intraday_return_next_session','price_source','label_available_at','input_contract_version','input_quality','updated_at']]
    return f"""
WITH calendar AS (
  SELECT trade_date,row_number() OVER (ORDER BY trade_date) session_no
  FROM market.trading_calendar WHERE exchange='SSE' AND session_status='open'
    AND (is_verified OR source_name='observed market bars fallback')
), prices AS (
  SELECT b.asset_id,b.dt,b.source,a.asset_type,cal.session_no,
    lead(b.dt,1) OVER w AS entry_date,lead(b.open,1) OVER w AS entry_open,
    lead(cal.session_no,1) OVER w AS entry_session,
    {','.join(lead_columns)}
  FROM market.canonical_total_return_daily_v2 b JOIN market.assets a USING(asset_id)
  JOIN calendar cal ON cal.trade_date=b.dt
  LEFT JOIN market.adjustment_factors_daily adj ON adj.asset_id=b.asset_id AND adj.dt=b.dt
  WHERE b.dt>=%(since)s::date AND b.dt<%(until)s::date+151
    AND a.asset_type IN ('stock','index') AND NOT (a.metadata ? 'identity_alias_of')
  WINDOW w AS (PARTITION BY b.asset_id ORDER BY b.dt)
), selected AS (
  SELECT * FROM prices WHERE dt BETWEEN %(since)s AND %(until)s
)
INSERT INTO factor.forward_returns (
  asset_id,signal_date,entry_date,entry_price,{','.join(label_cols)},
  intraday_return_next_session,price_source,label_available_at,input_contract_version,input_quality,updated_at
)
SELECT asset_id,dt,entry_date,entry_open,{','.join(values)},
  CASE WHEN {valid[1]} THEN close_1d/entry_open-1 END,
  source,
  CASE WHEN {valid[60]} THEN greatest(source_available_60d,(exit_date_60d::timestamp+time '15:05') AT TIME ZONE 'Asia/Shanghai') END,
  'pit-input-v1',jsonb_build_object('contract_version','pit-input-v1',
    'one_day_stock_label','diagnostic_intraday_only_not_T1_executable',
    'horizon_clock','verified_exchange_sessions','requires_verified_adjustments',true),now()
FROM selected
ON CONFLICT(asset_id,signal_date) DO UPDATE SET {','.join(updates)}
"""


LABEL_SQL = build_label_sql()


def date_chunks(start: dt.date, end: dt.date, days: int):
    current = start
    while current <= end:
        chunk_end = min(end, current + dt.timedelta(days=days - 1))
        yield current, chunk_end
        current = chunk_end + dt.timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--chunk-days", type=int, default=31)
    parser.add_argument("--max-decompressed-tuples", type=int, default=1_000_000)
    parser.add_argument("--skip-labels", action="store_true")
    parser.add_argument("--knowledge-mode", choices=("system","public"), default="system")
    parser.add_argument("--decision-time", default="23:59:59", help="Beijing daily cutoff, capped at actual run time.")
    parser.add_argument("--labels-only", action="store_true")
    args = parser.parse_args()
    dt.time.fromisoformat(args.decision_time)
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since must not be after --until")
    if args.skip_labels and args.labels_only:
        parser.error("--skip-labels and --labels-only cannot be used together")
    if args.max_decompressed_tuples < 0:
        parser.error("--max-decompressed-tuples must be non-negative")

    with connect(autocommit=True) as conn:
        conn.execute("SELECT set_config('finagent.knowledge_mode',%s,false)",(args.knowledge_mode,))
        conn.execute("SELECT set_config('finagent.decision_time',%s,false)",(args.decision_time,))
        conn.execute(
            "SELECT set_config('timescaledb.max_tuples_decompressed_per_dml_transaction', %s, false)",
            (str(args.max_decompressed_tuples),),
        )
        for chunk_start, chunk_end in date_chunks(since, until, max(1, args.chunk_days)):
            with conn.cursor() as cur:
                params = {"since": chunk_start, "until": chunk_end}
                feature_rows = 0
                if not args.labels_only:
                    cur.execute(FEATURE_SQL, params)
                    feature_rows = cur.rowcount
                label_rows = 0
                if not args.skip_labels:
                    cur.execute(LABEL_SQL, params)
                    label_rows = cur.rowcount
            print(f"{chunk_start}..{chunk_end} features={feature_rows} labels={label_rows}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
