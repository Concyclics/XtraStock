BEGIN;

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS analytics.asset_research_daily (
  asset_id uuid REFERENCES market.assets(asset_id) ON DELETE SET NULL,
  stock_code text NOT NULL,
  stock_name text,
  dt date NOT NULL,
  report_count integer NOT NULL DEFAULT 0,
  distinct_institution_count integer NOT NULL DEFAULT 0,
  buy_count integer NOT NULL DEFAULT 0,
  overweight_count integer NOT NULL DEFAULT 0,
  positive_rating_count integer NOT NULL DEFAULT 0,
  upgrade_count integer NOT NULL DEFAULT 0,
  downgrade_count integer NOT NULL DEFAULT 0,
  first_coverage_count integer NOT NULL DEFAULT 0,
  rating_score_avg double precision,
  rating_score_max double precision,
  institution_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_document_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (stock_code, dt)
);

COMMENT ON TABLE analytics.asset_research_daily IS 'Daily stock-level research-report momentum and rating aggregates.';
CREATE INDEX IF NOT EXISTS asset_research_daily_asset_dt_idx ON analytics.asset_research_daily (asset_id, dt DESC);
CREATE INDEX IF NOT EXISTS asset_research_daily_dt_idx ON analytics.asset_research_daily (dt DESC);
CREATE INDEX IF NOT EXISTS asset_research_daily_positive_idx ON analytics.asset_research_daily (positive_rating_count DESC, report_count DESC);
CREATE INDEX IF NOT EXISTS asset_research_daily_updated_dt_idx
  ON analytics.asset_research_daily (updated_at, dt);

CREATE TABLE IF NOT EXISTS analytics.industry_research_daily (
  industry_name text NOT NULL,
  dt date NOT NULL,
  report_count integer NOT NULL DEFAULT 0,
  distinct_institution_count integer NOT NULL DEFAULT 0,
  buy_count integer NOT NULL DEFAULT 0,
  overweight_count integer NOT NULL DEFAULT 0,
  positive_rating_count integer NOT NULL DEFAULT 0,
  upgrade_count integer NOT NULL DEFAULT 0,
  downgrade_count integer NOT NULL DEFAULT 0,
  first_coverage_count integer NOT NULL DEFAULT 0,
  rating_score_avg double precision,
  rating_score_max double precision,
  institution_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_document_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (industry_name, dt)
);

COMMENT ON TABLE analytics.industry_research_daily IS 'Daily industry-level research-report momentum and rating aggregates.';
CREATE INDEX IF NOT EXISTS industry_research_daily_dt_idx ON analytics.industry_research_daily (dt DESC);
CREATE INDEX IF NOT EXISTS industry_research_daily_positive_idx ON analytics.industry_research_daily (positive_rating_count DESC, report_count DESC);

CREATE TABLE IF NOT EXISTS analytics.institution_research_daily (
  institution text NOT NULL,
  target_type text NOT NULL,
  target_key text NOT NULL,
  target_name text,
  dt date NOT NULL,
  report_count integer NOT NULL DEFAULT 0,
  positive_rating_count integer NOT NULL DEFAULT 0,
  upgrade_count integer NOT NULL DEFAULT 0,
  downgrade_count integer NOT NULL DEFAULT 0,
  first_coverage_count integer NOT NULL DEFAULT 0,
  rating_score_avg double precision,
  source_document_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (institution, target_type, target_key, dt),
  CONSTRAINT institution_research_daily_target_chk CHECK (target_type IN ('stock', 'industry'))
);

COMMENT ON TABLE analytics.institution_research_daily IS 'Daily institution-by-target research actions for finding broker/research-house behavior changes.';
CREATE INDEX IF NOT EXISTS institution_research_daily_target_dt_idx ON analytics.institution_research_daily (target_type, target_key, dt DESC);
CREATE INDEX IF NOT EXISTS institution_research_daily_inst_dt_idx ON analytics.institution_research_daily (institution, dt DESC);

CREATE TABLE IF NOT EXISTS analytics.asset_forecast_daily (
  asset_id uuid REFERENCES market.assets(asset_id) ON DELETE SET NULL,
  stock_code text NOT NULL,
  stock_name text,
  as_of_date date NOT NULL,
  latest_report_date date,
  forecast_report_date date,
  forecast_document_id uuid,
  forecast_year integer,
  forecast_net_profit numeric,
  forecast_eps numeric,
  next_forecast_year integer,
  next_forecast_net_profit numeric,
  next_forecast_eps numeric,
  later_forecast_year integer,
  later_forecast_net_profit numeric,
  later_forecast_eps numeric,
  forecast_net_profit_growth double precision,
  forecast_eps_growth double precision,
  forward_pe numeric,
  next_forward_pe numeric,
  latest_rating text,
  latest_rating_change text,
  forecast_report_count_180d integer NOT NULL DEFAULT 0,
  forecast_institution_count_180d integer NOT NULL DEFAULT 0,
  consensus_net_profit numeric,
  consensus_eps numeric,
  consensus_net_profit_median numeric,
  consensus_eps_median numeric,
  consensus_net_profit_stddev numeric,
  consensus_eps_stddev numeric,
  consensus_pe numeric,
  actual_annual_report_date date,
  actual_annual_net_profit numeric,
  forecast_vs_actual_growth double precision,
  source_document_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (stock_code, as_of_date)
);

COMMENT ON TABLE analytics.asset_forecast_daily IS 'Full-market stock forecast layer from research reports: latest forecast, consensus forecast, forward PE, and comparable actual annual net profit where available.';
CREATE INDEX IF NOT EXISTS asset_forecast_daily_asset_date_idx ON analytics.asset_forecast_daily (asset_id, as_of_date DESC);
CREATE INDEX IF NOT EXISTS asset_forecast_daily_date_growth_idx ON analytics.asset_forecast_daily (as_of_date DESC, forecast_net_profit_growth DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS asset_forecast_daily_date_pe_idx ON analytics.asset_forecast_daily (as_of_date DESC, forward_pe ASC NULLS LAST);

CREATE TABLE IF NOT EXISTS analytics.asset_technical_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  dt date NOT NULL,
  close double precision,
  pct_chg_1d double precision,
  ret_5d double precision,
  ret_20d double precision,
  ma5 double precision,
  ma10 double precision,
  ma20 double precision,
  ma60 double precision,
  volume double precision,
  amount double precision,
  volume_ratio_20d double precision,
  amount_ratio_20d double precision,
  volatility_20d double precision,
  drawdown_20d double precision,
  new_high_20d boolean NOT NULL DEFAULT false,
  new_low_20d boolean NOT NULL DEFAULT false,
  local_peak boolean NOT NULL DEFAULT false,
  local_trough boolean NOT NULL DEFAULT false,
  breakout_20d boolean NOT NULL DEFAULT false,
  pattern_tags text[] NOT NULL DEFAULT '{}'::text[],
  source text NOT NULL DEFAULT 'market.bars_daily',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, dt)
);

COMMENT ON TABLE analytics.asset_technical_daily IS 'Daily technical features from OHLCV bars: returns, moving averages, volume ratios, breakouts, peaks, and troughs.';
CREATE INDEX IF NOT EXISTS asset_technical_daily_dt_idx ON analytics.asset_technical_daily (dt DESC);
CREATE INDEX IF NOT EXISTS asset_technical_daily_tags_idx ON analytics.asset_technical_daily USING gin (pattern_tags);
CREATE INDEX IF NOT EXISTS asset_technical_daily_updated_dt_idx
  ON analytics.asset_technical_daily (updated_at, dt);

CREATE TABLE IF NOT EXISTS analytics.document_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid REFERENCES corpus.documents(id) ON DELETE CASCADE,
  event_date date,
  event_type text NOT NULL,
  polarity smallint NOT NULL DEFAULT 0,
  impact_score double precision,
  confidence double precision,
  entity_texts text[] NOT NULL DEFAULT '{}'::text[],
  asset_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  stock_codes text[] NOT NULL DEFAULT '{}'::text[],
  industries text[] NOT NULL DEFAULT '{}'::text[],
  keywords text[] NOT NULL DEFAULT '{}'::text[],
  summary text,
  evidence text,
  extraction_method text NOT NULL,
  extraction_run_id uuid REFERENCES ai.extraction_runs(id) ON DELETE SET NULL,
  event_hash text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_events_polarity_chk CHECK (polarity IN (-1, 0, 1))
);

COMMENT ON TABLE analytics.document_events IS 'Structured policy, industry, company, demand, price, risk, and rating events extracted from documents.';
CREATE UNIQUE INDEX IF NOT EXISTS document_events_hash_uniq ON analytics.document_events (event_hash);
CREATE INDEX IF NOT EXISTS document_events_date_type_idx ON analytics.document_events (event_date DESC, event_type);
CREATE INDEX IF NOT EXISTS document_events_document_idx ON analytics.document_events (document_id);
CREATE INDEX IF NOT EXISTS document_events_stock_codes_idx ON analytics.document_events USING gin (stock_codes);
CREATE INDEX IF NOT EXISTS document_events_industries_idx ON analytics.document_events USING gin (industries);
CREATE INDEX IF NOT EXISTS document_events_keywords_idx ON analytics.document_events USING gin (keywords);
CREATE INDEX IF NOT EXISTS document_events_updated_date_idx
  ON analytics.document_events (updated_at, event_date);

CREATE TABLE IF NOT EXISTS analytics.asset_signal_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  dt date NOT NULL,
  stock_code text,
  stock_name text,
  research_score double precision NOT NULL DEFAULT 0,
  rating_score double precision NOT NULL DEFAULT 0,
  technical_score double precision NOT NULL DEFAULT 0,
  news_score double precision NOT NULL DEFAULT 0,
  policy_score double precision NOT NULL DEFAULT 0,
  composite_score double precision NOT NULL DEFAULT 0,
  signal_tags text[] NOT NULL DEFAULT '{}'::text[],
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, dt)
);

COMMENT ON TABLE analytics.asset_signal_daily IS 'Agent-facing daily fused stock signals from research momentum, ratings, news/events, and market technical features.';
CREATE INDEX IF NOT EXISTS asset_signal_daily_dt_score_idx ON analytics.asset_signal_daily (dt DESC, composite_score DESC);
CREATE INDEX IF NOT EXISTS asset_signal_daily_tags_idx ON analytics.asset_signal_daily USING gin (signal_tags);

CREATE TABLE IF NOT EXISTS analytics.signal_alerts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  signal_date date NOT NULL,
  signal_type text NOT NULL,
  target_type text NOT NULL,
  target_key text NOT NULL,
  target_name text,
  score double precision,
  title text NOT NULL,
  summary text,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL DEFAULT 'new',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT signal_alerts_status_chk CHECK (status IN ('new', 'reviewed', 'ignored', 'archived'))
);

COMMENT ON TABLE analytics.signal_alerts IS 'Deduplicated notable signals for analyst or agent review.';
CREATE UNIQUE INDEX IF NOT EXISTS signal_alerts_identity_uniq
  ON analytics.signal_alerts (signal_date, signal_type, target_type, target_key);
CREATE INDEX IF NOT EXISTS signal_alerts_date_score_idx ON analytics.signal_alerts (signal_date DESC, score DESC);

CREATE TABLE IF NOT EXISTS analytics.ai_hardware_terms (
  term_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  term text NOT NULL UNIQUE,
  normalized_term text NOT NULL,
  subindustry text NOT NULL,
  category text NOT NULL,
  aliases text[] NOT NULL DEFAULT '{}'::text[],
  ai_hardware_relevance double precision NOT NULL DEFAULT 0,
  relevance_reason text,
  is_seed boolean NOT NULL DEFAULT true,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE analytics.ai_hardware_terms IS 'AI hardware watchlist terms and fine-grained subindustry mapping, such as MLCC, MPO, PCB, glass substrate, tungsten hexafluoride, and specialty gases.';
CREATE INDEX IF NOT EXISTS ai_hardware_terms_subindustry_idx ON analytics.ai_hardware_terms (subindustry);
CREATE INDEX IF NOT EXISTS ai_hardware_terms_category_idx ON analytics.ai_hardware_terms (category);
CREATE INDEX IF NOT EXISTS ai_hardware_terms_aliases_idx ON analytics.ai_hardware_terms USING gin (aliases);

CREATE TABLE IF NOT EXISTS analytics.ai_hardware_mentions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  term_id uuid NOT NULL REFERENCES analytics.ai_hardware_terms(term_id) ON DELETE CASCADE,
  document_id uuid REFERENCES corpus.documents(id) ON DELETE CASCADE,
  mention_date date,
  source_type text,
  source_name text,
  stock_codes text[] NOT NULL DEFAULT '{}'::text[],
  stock_names text[] NOT NULL DEFAULT '{}'::text[],
  industries text[] NOT NULL DEFAULT '{}'::text[],
  evidence text,
  ai_context_terms text[] NOT NULL DEFAULT '{}'::text[],
  ai_hardware_relevance double precision,
  extraction_method text NOT NULL DEFAULT 'rules:v1',
  mention_hash text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE analytics.ai_hardware_mentions IS 'Document-level mentions of AI hardware terms, with evidence snippets, linked stocks, and AI-context score.';
CREATE UNIQUE INDEX IF NOT EXISTS ai_hardware_mentions_hash_uniq ON analytics.ai_hardware_mentions (mention_hash);
CREATE INDEX IF NOT EXISTS ai_hardware_mentions_date_idx ON analytics.ai_hardware_mentions (mention_date DESC);
CREATE INDEX IF NOT EXISTS ai_hardware_mentions_term_date_idx ON analytics.ai_hardware_mentions (term_id, mention_date DESC);
CREATE INDEX IF NOT EXISTS ai_hardware_mentions_stock_codes_idx ON analytics.ai_hardware_mentions USING gin (stock_codes);

CREATE TABLE IF NOT EXISTS analytics.ai_hardware_asset_exposure (
  asset_id uuid REFERENCES market.assets(asset_id) ON DELETE SET NULL,
  stock_code text NOT NULL,
  stock_name text,
  term_id uuid NOT NULL REFERENCES analytics.ai_hardware_terms(term_id) ON DELETE CASCADE,
  subindustry text NOT NULL,
  as_of_date date NOT NULL,
  mention_count_30d integer NOT NULL DEFAULT 0,
  mention_count_90d integer NOT NULL DEFAULT 0,
  research_report_count_180d integer NOT NULL DEFAULT 0,
  latest_report_date date,
  latest_rating text,
  latest_rating_change text,
  forecast_year integer,
  forecast_net_profit numeric,
  forecast_eps numeric,
  previous_forecast_year integer,
  previous_forecast_net_profit numeric,
  previous_forecast_eps numeric,
  forecast_net_profit_growth double precision,
  forecast_eps_growth double precision,
  forward_pe numeric,
  target_price numeric,
  exposure_score double precision NOT NULL DEFAULT 0,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (stock_code, term_id, as_of_date)
);

COMMENT ON TABLE analytics.ai_hardware_asset_exposure IS 'Company-level exposure to AI hardware fine-grained terms, joined with research momentum, net-profit forecasts, and forward PE.';
CREATE INDEX IF NOT EXISTS ai_hardware_asset_exposure_score_idx ON analytics.ai_hardware_asset_exposure (as_of_date DESC, exposure_score DESC);
CREATE INDEX IF NOT EXISTS ai_hardware_asset_exposure_subindustry_idx ON analytics.ai_hardware_asset_exposure (subindustry, as_of_date DESC);

CREATE TABLE IF NOT EXISTS analytics.ai_hardware_subindustry_daily (
  subindustry text NOT NULL,
  dt date NOT NULL,
  mention_count integer NOT NULL DEFAULT 0,
  distinct_document_count integer NOT NULL DEFAULT 0,
  distinct_stock_count integer NOT NULL DEFAULT 0,
  positive_event_score double precision NOT NULL DEFAULT 0,
  negative_event_score double precision NOT NULL DEFAULT 0,
  research_report_count integer NOT NULL DEFAULT 0,
  positive_rating_count integer NOT NULL DEFAULT 0,
  avg_forward_pe numeric,
  avg_forecast_net_profit_growth double precision,
  avg_forecast_eps_growth double precision,
  top_terms text[] NOT NULL DEFAULT '{}'::text[],
  top_stocks jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (subindustry, dt)
);

COMMENT ON TABLE analytics.ai_hardware_subindustry_daily IS 'Fine-grained AI hardware subindustry daily trend view with mentions, linked stocks, research momentum, and valuation/forecast aggregates.';
CREATE INDEX IF NOT EXISTS ai_hardware_subindustry_daily_dt_idx ON analytics.ai_hardware_subindustry_daily (dt DESC);
CREATE INDEX IF NOT EXISTS ai_hardware_subindustry_daily_mentions_idx ON analytics.ai_hardware_subindustry_daily (mention_count DESC, distinct_stock_count DESC);

GRANT USAGE ON SCHEMA analytics TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA analytics TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA analytics TO finance_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;

COMMIT;
