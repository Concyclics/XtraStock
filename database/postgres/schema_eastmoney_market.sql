BEGIN;

CREATE SCHEMA IF NOT EXISTS company;
CREATE SCHEMA IF NOT EXISTS macro;

COMMENT ON SCHEMA company IS 'Company fundamentals and financial metrics from market data providers.';
COMMENT ON SCHEMA macro IS 'Macroeconomic indicators and time-series values.';

CREATE TABLE IF NOT EXISTS market.asset_memberships (
  parent_asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  member_asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  membership_type text NOT NULL,
  weight numeric,
  source text NOT NULL DEFAULT 'eastmoney',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (parent_asset_id, member_asset_id, membership_type, source)
);

COMMENT ON TABLE market.asset_memberships IS 'Constituent membership for indices, industry boards, concept boards, and future baskets.';
CREATE INDEX IF NOT EXISTS asset_memberships_member_idx ON market.asset_memberships (member_asset_id);
CREATE INDEX IF NOT EXISTS asset_memberships_type_idx ON market.asset_memberships (membership_type);

CREATE TABLE IF NOT EXISTS market.quote_snapshots (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  ts timestamptz NOT NULL,
  price numeric,
  open numeric,
  high numeric,
  low numeric,
  pre_close numeric,
  avg_price numeric,
  volume numeric,
  amount numeric,
  pct_chg numeric,
  chg numeric,
  amplitude numeric,
  turnover numeric,
  vol_ratio numeric,
  pe_ttm numeric,
  pb numeric,
  total_mv numeric,
  float_mv numeric,
  wei_bi numeric,
  source text NOT NULL DEFAULT 'eastmoney:snapshot',
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, ts, source)
);

COMMENT ON TABLE market.quote_snapshots IS 'Intraday quote snapshots, including valuation, turnover, market cap, and source raw payload.';
CREATE INDEX IF NOT EXISTS quote_snapshots_ts_idx ON market.quote_snapshots (ts DESC);

CREATE TABLE IF NOT EXISTS market.order_book_snapshots (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  ts timestamptz NOT NULL,
  bid1 numeric,
  bid1_volume numeric,
  bid2 numeric,
  bid2_volume numeric,
  bid3 numeric,
  bid3_volume numeric,
  bid4 numeric,
  bid4_volume numeric,
  bid5 numeric,
  bid5_volume numeric,
  ask1 numeric,
  ask1_volume numeric,
  ask2 numeric,
  ask2_volume numeric,
  ask3 numeric,
  ask3_volume numeric,
  ask4 numeric,
  ask4_volume numeric,
  ask5 numeric,
  ask5_volume numeric,
  wei_bi numeric,
  wei_cha numeric,
  source text NOT NULL DEFAULT 'eastmoney:orderbook',
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, ts, source)
);

COMMENT ON TABLE market.order_book_snapshots IS 'Best five bid/ask order book snapshots when available from the public quote API.';
CREATE INDEX IF NOT EXISTS order_book_snapshots_ts_idx ON market.order_book_snapshots (ts DESC);

CREATE TABLE IF NOT EXISTS market.trade_ticks (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  trade_date date NOT NULL,
  ts timestamptz NOT NULL,
  price double precision,
  volume double precision,
  direction integer,
  source text NOT NULL DEFAULT 'eastmoney:ticks',
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, ts, price, volume, direction, source)
);

COMMENT ON TABLE market.trade_ticks IS 'Public intraday trade ticks. Direction follows Eastmoney: 1 buy, 2 sell, 4 auction where provided.';
CREATE INDEX IF NOT EXISTS trade_ticks_date_idx ON market.trade_ticks (trade_date DESC);
CREATE INDEX IF NOT EXISTS trade_ticks_ts_idx ON market.trade_ticks (ts DESC);

CREATE TABLE IF NOT EXISTS market.money_flow_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  ts date NOT NULL,
  main_net numeric,
  small_net numeric,
  mid_net numeric,
  big_net numeric,
  super_net numeric,
  source text NOT NULL DEFAULT 'eastmoney:fundflow:day',
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, ts, source)
);

COMMENT ON TABLE market.money_flow_daily IS 'Daily Eastmoney money flow net amounts by order-size bucket.';
CREATE INDEX IF NOT EXISTS money_flow_daily_ts_idx ON market.money_flow_daily (ts DESC);

CREATE TABLE IF NOT EXISTS company.financial_metrics (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  report_date date NOT NULL,
  metric_name text NOT NULL,
  metric_value numeric,
  source text NOT NULL DEFAULT 'eastmoney:fundamental',
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, report_date, metric_name, source)
);

COMMENT ON TABLE company.financial_metrics IS 'Normalized company financial metrics from Eastmoney financial reports.';
CREATE INDEX IF NOT EXISTS financial_metrics_metric_idx ON company.financial_metrics (metric_name, report_date DESC);

CREATE TABLE IF NOT EXISTS market.events_raw (
  id bigserial PRIMARY KEY,
  category text NOT NULL,
  asset_id uuid REFERENCES market.assets(asset_id) ON DELETE SET NULL,
  event_date date,
  source text NOT NULL DEFAULT 'eastmoney:datacenter',
  source_hash text NOT NULL,
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE market.events_raw IS 'Raw Eastmoney datacenter event rows, kept in JSONB for later rule/LLM materialization.';
CREATE INDEX IF NOT EXISTS events_raw_category_date_idx ON market.events_raw (category, event_date DESC);
CREATE INDEX IF NOT EXISTS events_raw_asset_idx ON market.events_raw (asset_id);
CREATE UNIQUE INDEX IF NOT EXISTS events_raw_identity_uniq
  ON market.events_raw (
    category,
    COALESCE(asset_id, '00000000-0000-0000-0000-000000000000'::uuid),
    COALESCE(event_date, '1900-01-01'::date),
    source_hash,
    source
  );

CREATE TABLE IF NOT EXISTS macro.indicators (
  indicator_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  indicator_code text NOT NULL UNIQUE,
  indicator_name text,
  source text NOT NULL DEFAULT 'eastmoney',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE macro.indicators IS 'Dictionary of macro indicator codes, sources, and metadata.';

CREATE TABLE IF NOT EXISTS macro.indicator_values (
  indicator_id uuid NOT NULL REFERENCES macro.indicators(indicator_id) ON DELETE CASCADE,
  period text NOT NULL,
  observed_at date,
  value numeric,
  source text NOT NULL DEFAULT 'eastmoney',
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (indicator_id, period, source)
);

COMMENT ON TABLE macro.indicator_values IS 'Macro indicator observations. Raw JSON keeps source-specific columns for later normalization.';
CREATE INDEX IF NOT EXISTS indicator_values_observed_idx ON macro.indicator_values (observed_at DESC);

CREATE TABLE IF NOT EXISTS market.crawl_state (
  job_name text NOT NULL,
  target_key text NOT NULL,
  source text NOT NULL DEFAULT 'eastmoney',
  last_cursor text,
  status text NOT NULL DEFAULT 'ok',
  error_count integer NOT NULL DEFAULT 0,
  last_error text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_name, target_key, source),
  CONSTRAINT crawl_state_status_chk CHECK (status IN ('ok', 'failed', 'skipped', 'running'))
);

COMMENT ON TABLE market.crawl_state IS 'Crawler cursors and last status per job and target.';
CREATE INDEX IF NOT EXISTS crawl_state_updated_idx ON market.crawl_state (updated_at DESC);

CREATE INDEX IF NOT EXISTS bars_daily_created_ts_idx
  ON market.bars_daily (created_at, ts);
ALTER TABLE market.bars_daily
  ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE market.bars_daily
  ALTER COLUMN updated_at SET DEFAULT now();
CREATE INDEX IF NOT EXISTS bars_daily_updated_ts_idx
  ON market.bars_daily (updated_at DESC, ts DESC);
CREATE INDEX IF NOT EXISTS money_flow_daily_created_ts_idx
  ON market.money_flow_daily (created_at, ts);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.proname = 'create_hypertable'
      AND n.nspname = 'public'
  ) THEN
    PERFORM public.create_hypertable('market.quote_snapshots', 'ts', if_not_exists => TRUE);
    PERFORM public.create_hypertable('market.order_book_snapshots', 'ts', if_not_exists => TRUE);
    PERFORM public.create_hypertable('market.trade_ticks', 'ts', if_not_exists => TRUE);
  END IF;
END
$$;

GRANT USAGE ON SCHEMA company, macro TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA company, macro TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA company, macro TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA market TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA market TO finance_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA company GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA macro GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA company GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA macro GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;

COMMIT;
