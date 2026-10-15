BEGIN;

CREATE SCHEMA IF NOT EXISTS factor;

CREATE TABLE IF NOT EXISTS factor.definitions (
  factor_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  factor_name text NOT NULL UNIQUE,
  display_name text NOT NULL,
  family text NOT NULL,
  description text NOT NULL,
  direction smallint NOT NULL DEFAULT 1,
  expression jsonb NOT NULL,
  required_features text[] NOT NULL DEFAULT '{}'::text[],
  lifecycle_status text NOT NULL DEFAULT 'candidate',
  version integer NOT NULL DEFAULT 1,
  owner text NOT NULL DEFAULT 'system',
  promotion_gates jsonb NOT NULL DEFAULT jsonb_build_object(
    'min_test_dates', 40, 'min_coverage', 0.60,
    'min_abs_rank_ic', 0.02, 'max_q_value', 0.10,
    'min_sign_consistency', 0.55, 'min_rank_ic_ir', 0.20,
    'min_top_bottom_spread', 0.0, 'max_turnover', 0.50
  ),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT factor_direction_chk CHECK (direction IN (-1, 1)),
  CONSTRAINT factor_lifecycle_chk CHECK (lifecycle_status IN ('candidate', 'shadow', 'production', 'retired'))
);

COMMENT ON TABLE factor.definitions IS
  'Versioned safe-DSL factor registry. expression is compiled from a whitelist and is never arbitrary SQL.';

CREATE TABLE IF NOT EXISTS factor.feature_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  dt date NOT NULL,
  momentum_20d double precision,
  reversal_5d double precision,
  low_volatility_20d double precision,
  volume_surprise_20d double precision,
  amount_surprise_20d double precision,
  drawdown_20d double precision,
  money_flow_intensity_5d double precision,
  research_breadth_20d double precision,
  rating_upgrade_20d double precision,
  event_sentiment_5d double precision,
  graph_direct_exposure double precision,
  graph_event_impulse_30d double precision,
  graph_attention_30d double precision,
  graph_novelty_30d double precision,
  graph_rating_momentum_60d double precision,
  graph_forecast_np_growth double precision,
  graph_forecast_eps_growth double precision,
  graph_forecast_revenue_growth double precision,
  graph_forecast_np_revision_90d double precision,
  graph_forecast_eps_revision_90d double precision,
  graph_forecast_revenue_revision_90d double precision,
  graph_forecast_dispersion_180d double precision,
  graph_forward_earnings_yield double precision,
  graph_target_price_upside double precision,
  graph_policy_impulse_30d double precision,
  graph_demand_impulse_30d double precision,
  graph_order_impulse_30d double precision,
  graph_pricing_impulse_30d double precision,
  graph_risk_impulse_30d double precision,
  graph_source_diversity_30d double precision,
  graph_two_hop_exposure double precision,
  graph_centrality double precision,
  graph_evidence_diversity double precision,
  graph_relation_stability double precision,
  available_at timestamptz NOT NULL,
  source_dates jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, dt)
);

COMMENT ON TABLE factor.feature_daily IS
  'Wide point-in-time feature matrix. Wide storage avoids duplicating asset/date keys for every baseline feature.';
CREATE INDEX IF NOT EXISTS factor_feature_daily_dt_idx ON factor.feature_daily (dt DESC, asset_id);
CREATE INDEX IF NOT EXISTS factor_feature_daily_updated_dt_idx
  ON factor.feature_daily (updated_at, dt);

ALTER TABLE factor.feature_daily
  ADD COLUMN IF NOT EXISTS graph_rating_momentum_60d double precision,
  ADD COLUMN IF NOT EXISTS graph_forecast_np_growth double precision,
  ADD COLUMN IF NOT EXISTS graph_forecast_eps_growth double precision,
  ADD COLUMN IF NOT EXISTS graph_forecast_revenue_growth double precision,
  ADD COLUMN IF NOT EXISTS graph_forecast_np_revision_90d double precision,
  ADD COLUMN IF NOT EXISTS graph_forecast_eps_revision_90d double precision,
  ADD COLUMN IF NOT EXISTS graph_forecast_revenue_revision_90d double precision,
  ADD COLUMN IF NOT EXISTS graph_forecast_dispersion_180d double precision,
  ADD COLUMN IF NOT EXISTS graph_forward_earnings_yield double precision,
  ADD COLUMN IF NOT EXISTS graph_target_price_upside double precision,
  ADD COLUMN IF NOT EXISTS graph_policy_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS graph_demand_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS graph_order_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS graph_pricing_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS graph_risk_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS graph_source_diversity_30d double precision,
  ADD COLUMN IF NOT EXISTS graph_two_hop_exposure double precision,
  ADD COLUMN IF NOT EXISTS graph_centrality double precision,
  ADD COLUMN IF NOT EXISTS graph_evidence_diversity double precision,
  ADD COLUMN IF NOT EXISTS graph_relation_stability double precision;

CREATE TABLE IF NOT EXISTS factor.forward_returns (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  signal_date date NOT NULL,
  entry_date date,
  entry_price double precision,
  exit_date_1d date,
  exit_date_5d date,
  exit_date_20d date,
  exit_price_1d double precision,
  exit_price_5d double precision,
  exit_price_20d double precision,
  return_1d double precision,
  return_5d double precision,
  return_20d double precision,
  benchmark_return_1d double precision,
  benchmark_return_5d double precision,
  benchmark_return_20d double precision,
  price_source text NOT NULL,
  label_available_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, signal_date)
);

COMMENT ON TABLE factor.forward_returns IS
  'Training labels only. Never join rows whose label_available_at is after an experiment cutoff.';
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS entry_price double precision;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_date_1d date;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_date_5d date;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_date_20d date;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_price_1d double precision;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_price_5d double precision;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_price_20d double precision;
CREATE INDEX IF NOT EXISTS factor_forward_returns_date_idx ON factor.forward_returns (signal_date DESC, asset_id);

CREATE TABLE IF NOT EXISTS factor.experiment_runs (
  run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_type text NOT NULL DEFAULT 'walk_forward',
  train_start date NOT NULL,
  train_end date NOT NULL,
  test_start date NOT NULL,
  test_end date NOT NULL,
  horizon_days integer NOT NULL,
  universe jsonb NOT NULL DEFAULT '{}'::jsonb,
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  code_version text,
  status text NOT NULL DEFAULT 'running',
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  error text,
  CONSTRAINT factor_run_status_chk CHECK (status IN ('running', 'complete', 'failed')),
  CONSTRAINT factor_run_dates_chk CHECK (train_start <= train_end AND train_end < test_start AND test_start <= test_end)
);

CREATE TABLE IF NOT EXISTS factor.evaluations (
  run_id uuid NOT NULL REFERENCES factor.experiment_runs(run_id) ON DELETE CASCADE,
  factor_id uuid NOT NULL REFERENCES factor.definitions(factor_id) ON DELETE CASCADE,
  sample_name text NOT NULL,
  observation_count bigint NOT NULL,
  date_count integer NOT NULL,
  coverage double precision,
  rank_ic_mean double precision,
  rank_ic_stddev double precision,
  rank_ic_ir double precision,
  sign_consistency double precision,
  top_bottom_spread_mean double precision,
  turnover_mean double precision,
  p_value double precision,
  q_value double precision,
  passed_gates boolean NOT NULL DEFAULT false,
  metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, factor_id, sample_name)
);

CREATE INDEX IF NOT EXISTS factor_evaluations_rank_idx
  ON factor.evaluations (sample_name, passed_gates DESC, rank_ic_mean DESC NULLS LAST);

CREATE TABLE IF NOT EXISTS factor.lifecycle_events (
  event_id bigserial PRIMARY KEY,
  factor_id uuid NOT NULL REFERENCES factor.definitions(factor_id) ON DELETE CASCADE,
  from_status text,
  to_status text NOT NULL,
  run_id uuid REFERENCES factor.experiment_runs(run_id) ON DELETE SET NULL,
  reason text NOT NULL,
  actor text NOT NULL DEFAULT 'system',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS factor.factor_health_daily (
  factor_id uuid NOT NULL REFERENCES factor.definitions(factor_id) ON DELETE CASCADE,
  dt date NOT NULL,
  lifecycle_status text NOT NULL,
  score_rows bigint NOT NULL,
  scored_assets integer NOT NULL,
  eligible_assets integer NOT NULL,
  coverage double precision NOT NULL,
  zero_value_ratio double precision,
  raw_mean double precision,
  raw_stddev double precision,
  raw_min double precision,
  raw_max double precision,
  latest_available_at timestamptz,
  source_feature_date date,
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (factor_id, dt),
  CONSTRAINT factor_health_lifecycle_chk
    CHECK (lifecycle_status IN ('candidate', 'shadow', 'production', 'retired')),
  CONSTRAINT factor_health_coverage_chk CHECK (coverage >= 0 AND coverage <= 1)
);

COMMENT ON TABLE factor.factor_health_daily IS
  'Small daily factor observability table used by interactive Agent tools. Avoids scanning compressed daily_scores.';
CREATE INDEX IF NOT EXISTS factor_health_daily_dt_idx
  ON factor.factor_health_daily (dt DESC, lifecycle_status, factor_id);

CREATE TABLE IF NOT EXISTS factor.production_approvals (
  approval_id bigserial PRIMARY KEY,
  factor_id uuid NOT NULL REFERENCES factor.definitions(factor_id) ON DELETE CASCADE,
  factor_version integer NOT NULL,
  evaluation_run_id uuid NOT NULL REFERENCES factor.experiment_runs(run_id) ON DELETE RESTRICT,
  decision text NOT NULL,
  approved_by text NOT NULL,
  reason text NOT NULL,
  effective_date date NOT NULL,
  expires_at timestamptz,
  checklist jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT factor_production_decision_chk CHECK (decision IN ('approved','rejected','revoked'))
);

COMMENT ON TABLE factor.production_approvals IS
  'Human approval audit for shadow-to-production transitions. Scheduled research jobs never write approvals.';
CREATE INDEX IF NOT EXISTS factor_production_approvals_factor_idx
  ON factor.production_approvals (factor_id, created_at DESC);

CREATE TABLE IF NOT EXISTS factor.daily_scores (
  factor_id uuid NOT NULL REFERENCES factor.definitions(factor_id) ON DELETE CASCADE,
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  dt date NOT NULL,
  raw_value double precision,
  percentile double precision,
  zscore double precision,
  available_at timestamptz NOT NULL,
  source_feature_date date NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (factor_id, asset_id, dt)
);

CREATE INDEX IF NOT EXISTS factor_daily_scores_date_idx
  ON factor.daily_scores (dt DESC, factor_id, percentile DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS factor_daily_scores_updated_dt_idx
  ON factor.daily_scores (updated_at, dt);

CREATE TABLE IF NOT EXISTS factor.backfill_partitions (
  job_name text NOT NULL,
  partition_start date NOT NULL,
  partition_end date NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  attempt_count integer NOT NULL DEFAULT 0,
  started_at timestamptz,
  finished_at timestamptz,
  output_text text,
  error_text text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_name, partition_start, partition_end),
  CONSTRAINT factor_backfill_status_chk CHECK (status IN ('pending','running','complete','failed'))
);

COMMENT ON TABLE factor.backfill_partitions IS
  'Restart-safe checkpoints for technical and factor history backfills.';

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb') THEN
    PERFORM public.create_hypertable(
      'factor.feature_daily', 'dt',
      chunk_time_interval => INTERVAL '90 days',
      migrate_data => TRUE, if_not_exists => TRUE
    );
    PERFORM public.create_hypertable(
      'factor.forward_returns', 'signal_date',
      chunk_time_interval => INTERVAL '90 days',
      migrate_data => TRUE, if_not_exists => TRUE
    );
    PERFORM public.create_hypertable(
      'factor.daily_scores', 'dt',
      chunk_time_interval => INTERVAL '90 days',
      migrate_data => TRUE, if_not_exists => TRUE
    );
    EXECUTE 'ALTER TABLE factor.feature_daily SET (
      timescaledb.compress,
      timescaledb.compress_segmentby = ''asset_id'',
      timescaledb.compress_orderby = ''dt DESC''
    )';
    EXECUTE 'ALTER TABLE factor.forward_returns SET (
      timescaledb.compress,
      timescaledb.compress_segmentby = ''asset_id'',
      timescaledb.compress_orderby = ''signal_date DESC''
    )';
    EXECUTE 'ALTER TABLE factor.daily_scores SET (
      timescaledb.compress,
      timescaledb.compress_segmentby = ''factor_id'',
      timescaledb.compress_orderby = ''dt DESC, asset_id''
    )';
    PERFORM public.add_compression_policy(
      'factor.feature_daily', INTERVAL '60 days', if_not_exists => TRUE
    );
    PERFORM public.add_compression_policy(
      'factor.forward_returns', INTERVAL '60 days', if_not_exists => TRUE
    );
    PERFORM public.add_compression_policy(
      'factor.daily_scores', INTERVAL '60 days', if_not_exists => TRUE
    );
  END IF;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'factor hypertable/compression setup skipped: %', SQLERRM;
END
$$;

INSERT INTO factor.definitions
  (factor_name, display_name, family, description, direction, expression, required_features, lifecycle_status, owner)
VALUES
  ('momentum_20d', '20日动量', 'price', '截至信号日收盘的20个交易日收益。', 1,
   '{"op":"field","field":"momentum_20d"}', ARRAY['momentum_20d'], 'shadow', 'baseline'),
  ('reversal_5d', '5日反转', 'price', '5日收益取反，检验短期反转。', 1,
   '{"op":"field","field":"reversal_5d"}', ARRAY['reversal_5d'], 'shadow', 'baseline'),
  ('low_volatility_20d', '20日低波', 'risk', '20日收益波动率取反。', 1,
   '{"op":"field","field":"low_volatility_20d"}', ARRAY['low_volatility_20d'], 'shadow', 'baseline'),
  ('volume_surprise_20d', '20日量能异常', 'liquidity', '相对过去20日均量的当日成交量异常。', 1,
   '{"op":"field","field":"volume_surprise_20d"}', ARRAY['volume_surprise_20d'], 'shadow', 'baseline'),
  ('money_flow_intensity_5d', '5日主力净流强度', 'flow', '5日主力净流入除以同期成交额。', 1,
   '{"op":"field","field":"money_flow_intensity_5d"}', ARRAY['money_flow_intensity_5d'], 'shadow', 'baseline'),
  ('research_breadth_20d', '20日研报覆盖广度', 'research', '20日研报数量与机构覆盖的对数缩放组合。', 1,
   '{"op":"field","field":"research_breadth_20d"}', ARRAY['research_breadth_20d'], 'shadow', 'baseline'),
  ('rating_upgrade_20d', '20日评级上调', 'research', '20日评级上调减下调的净强度。', 1,
   '{"op":"field","field":"rating_upgrade_20d"}', ARRAY['rating_upgrade_20d'], 'shadow', 'baseline'),
  ('event_sentiment_5d', '5日事件情绪', 'event', '近5日公司相关新闻事件的置信度加权方向。', 1,
   '{"op":"field","field":"event_sentiment_5d"}', ARRAY['event_sentiment_5d'], 'shadow', 'baseline'),
  ('graph_event_impulse_30d', '图谱事件脉冲', 'knowledge_graph', '知识图谱中近30日正负事件影响差。', 1,
   '{"op":"field","field":"graph_event_impulse_30d"}', ARRAY['graph_event_impulse_30d'], 'candidate', 'baseline'),
  ('graph_novelty_30d', '图谱关系新颖度', 'knowledge_graph', '近30日新关系及证据多样性。', 1,
   '{"op":"field","field":"graph_novelty_30d"}', ARRAY['graph_novelty_30d'], 'candidate', 'baseline'),
  ('graph_rating_momentum_60d', '60日评级动量', 'research', '机构评级上调、下调与评级方向的时点加权强度。', 1,
   '{"op":"field","field":"graph_rating_momentum_60d"}', ARRAY['graph_rating_momentum_60d'], 'candidate', 'temporal_kg:v1'),
  ('graph_forecast_eps_growth', '预测每股收益增速', 'forecast', '同一研报相邻预测年度的每股收益增速。', 1,
   '{"op":"field","field":"graph_forecast_eps_growth"}', ARRAY['graph_forecast_eps_growth'], 'candidate', 'temporal_kg:v1'),
  ('graph_forecast_eps_revision_90d', '90日盈利预测修正', 'forecast', '90日内同机构同年度每股收益相对修正。', 1,
   '{"op":"field","field":"graph_forecast_eps_revision_90d"}', ARRAY['graph_forecast_eps_revision_90d'], 'candidate', 'temporal_kg:v1'),
  ('graph_forecast_revenue_growth', '预测营收增速', 'forecast', '时点可见的一致预期营收同比增速。', 1,
   '{"op":"field","field":"graph_forecast_revenue_growth"}', ARRAY['graph_forecast_revenue_growth'], 'candidate', 'temporal_kg:v1'),
  ('graph_forecast_revenue_revision_90d', '90日营收预测修正', 'forecast', '90日内一致预期营收相对修正。', 1,
   '{"op":"field","field":"graph_forecast_revenue_revision_90d"}', ARRAY['graph_forecast_revenue_revision_90d'], 'candidate', 'temporal_kg:v1'),
  ('graph_forecast_dispersion_180d', '180日预测分歧度', 'forecast', '同一预测年度、每机构最新 EPS 预测的横截面离散度；不混用净利润总额。', -1,
   '{"op":"field","field":"graph_forecast_dispersion_180d"}', ARRAY['graph_forecast_dispersion_180d'], 'candidate', 'temporal_kg:v1'),
  ('graph_forward_earnings_yield', '远期盈利收益率', 'valuation', '明确年度的原研报 PE 倒数，价格口径为研报引用价格，非最新市价。', 1,
   '{"op":"field","field":"graph_forward_earnings_yield"}', ARRAY['graph_forward_earnings_yield'], 'candidate', 'temporal_kg:v1'),
  ('graph_target_price_upside', '目标价空间', 'research', '最新可见目标价相对当日收盘价的空间。', 1,
   '{"op":"field","field":"graph_target_price_upside"}', ARRAY['graph_target_price_upside'], 'candidate', 'temporal_kg:v1'),
  ('graph_demand_impulse_30d', '30日需求事件脉冲', 'event', '近30日需求类事件的置信度与影响加权方向。', 1,
   '{"op":"field","field":"graph_demand_impulse_30d"}', ARRAY['graph_demand_impulse_30d'], 'candidate', 'temporal_kg:v1'),
  ('graph_policy_impulse_30d', '30日政策事件脉冲', 'event', '近30日政策、政府投资和补贴事件的加权方向。', 1,
   '{"op":"field","field":"graph_policy_impulse_30d"}', ARRAY['graph_policy_impulse_30d'], 'candidate', 'temporal_kg:v1'),
  ('graph_order_impulse_30d', '30日订单事件脉冲', 'event', '近30日订单、中标和合同事件的加权方向。', 1,
   '{"op":"field","field":"graph_order_impulse_30d"}', ARRAY['graph_order_impulse_30d'], 'candidate', 'temporal_kg:v1'),
  ('graph_pricing_impulse_30d', '30日价格事件脉冲', 'event', '近30日涨价与降价事件的加权方向。', 1,
   '{"op":"field","field":"graph_pricing_impulse_30d"}', ARRAY['graph_pricing_impulse_30d'], 'candidate', 'temporal_kg:v1'),
  ('graph_risk_impulse_30d', '30日风险事件抑制', 'event', '近30日风险类事件负向强度取反。', -1,
   '{"op":"field","field":"graph_risk_impulse_30d"}', ARRAY['graph_risk_impulse_30d'], 'candidate', 'temporal_kg:v1'),
  ('graph_two_hop_exposure', '两跳产业链暴露', 'knowledge_graph', '公司经产品、材料和设备节点连接到主题的两跳暴露。', 1,
   '{"op":"field","field":"graph_two_hop_exposure"}', ARRAY['graph_two_hop_exposure'], 'candidate', 'temporal_kg:v1'),
  ('graph_evidence_diversity', '图谱证据多样性', 'knowledge_graph', '关系证据来源与机构多样性。', 1,
   '{"op":"field","field":"graph_evidence_diversity"}', ARRAY['graph_evidence_diversity'], 'candidate', 'temporal_kg:v1'),
  ('graph_source_diversity_30d', '30日事件来源多样性', 'knowledge_graph', '近30日事件证据来源数量。', 1,
   '{"op":"field","field":"graph_source_diversity_30d"}', ARRAY['graph_source_diversity_30d'], 'candidate', 'temporal_kg:v1'),
  ('graph_centrality', '图谱连接中心性', 'knowledge_graph', '时点可见关系度数的对数缩放。', 1,
   '{"op":"field","field":"graph_centrality"}', ARRAY['graph_centrality'], 'candidate', 'temporal_kg:v1'),
  ('graph_relation_stability', '图谱关系稳定性', 'knowledge_graph', '当前产业链关系持续时长的缩放均值。', 1,
   '{"op":"field","field":"graph_relation_stability"}', ARRAY['graph_relation_stability'], 'candidate', 'temporal_kg:v1')
ON CONFLICT (factor_name) DO UPDATE SET
  display_name=EXCLUDED.display_name,
  description=EXCLUDED.description,
  expression=EXCLUDED.expression,
  required_features=EXCLUDED.required_features,
  updated_at=now();

GRANT USAGE ON SCHEMA factor TO finance_app, finance_ai_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA factor TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA factor TO finance_app;
GRANT SELECT ON ALL TABLES IN SCHEMA factor TO finance_ai_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA factor GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA factor GRANT SELECT ON TABLES TO finance_ai_reader;

COMMIT;

-- Required by the versioned 60-session forecast baseline.
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS return_60d double precision;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_date_60d date;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_price_60d double precision;
