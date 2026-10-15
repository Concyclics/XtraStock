BEGIN;

CREATE SCHEMA IF NOT EXISTS analytics;

ALTER TABLE market.assets
  ADD COLUMN IF NOT EXISTS delisted_date date,
  ADD COLUMN IF NOT EXISTS lifecycle_status text NOT NULL DEFAULT 'active';

ALTER TABLE market.factors_daily
  ADD COLUMN IF NOT EXISTS available_at timestamptz;

ALTER TABLE research.reports
  ADD COLUMN IF NOT EXISTS available_at timestamptz;

ALTER TABLE company.financial_metrics
  ADD COLUMN IF NOT EXISTS available_at timestamptz,
  ADD COLUMN IF NOT EXISTS source_updated_at timestamptz;

ALTER TABLE company.financial_statement_metrics
  ADD COLUMN IF NOT EXISTS available_at timestamptz;

ALTER TABLE analytics.asset_technical_daily
  ADD COLUMN IF NOT EXISTS available_at timestamptz;

COMMENT ON COLUMN market.factors_daily.available_at IS
  'Earliest timestamp at which this observation may be used by a point-in-time analysis.';
COMMENT ON COLUMN research.reports.available_at IS
  'Publication/ingestion timestamp used to prevent report look-ahead.';
COMMENT ON COLUMN company.financial_metrics.available_at IS
  'Conservative public availability timestamp, preferably derived from NOTICE_DATE.';
COMMENT ON COLUMN company.financial_statement_metrics.available_at IS
  'Earliest evidence publication timestamp; do not substitute report period end.';
COMMENT ON COLUMN analytics.asset_technical_daily.available_at IS
  'Feature availability after the corresponding market close.';
COMMENT ON COLUMN analytics.asset_technical_daily.local_peak IS
  'Causal confirmation available on dt that the previous trading day was a local peak.';
COMMENT ON COLUMN analytics.asset_technical_daily.local_trough IS
  'Causal confirmation available on dt that the previous trading day was a local trough.';

CREATE INDEX IF NOT EXISTS factors_daily_available_idx
  ON market.factors_daily (available_at, factor_name, asset_id)
  WHERE available_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS reports_available_stock_idx
  ON research.reports (available_at, stock_code)
  WHERE available_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS financial_metrics_available_idx
  ON company.financial_metrics (asset_id, available_at DESC, report_date DESC)
  WHERE available_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS statement_metrics_available_idx
  ON company.financial_statement_metrics (asset_id, available_at DESC, report_date DESC)
  WHERE available_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS analytics.asset_price_source_policy (
  asset_id uuid PRIMARY KEY REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  price_source text NOT NULL,
  adjustment text NOT NULL,
  policy_version text NOT NULL DEFAULT 'canonical_price:v1',
  selected_at timestamptz NOT NULL DEFAULT now(),
  last_verified_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT asset_price_source_adjustment_chk
    CHECK (adjustment IN ('qfq', 'none'))
);

COMMENT ON TABLE analytics.asset_price_source_policy IS
  'One persistent daily-bar source per asset. Prevents adjusted/unadjusted prices from being mixed across dates.';
CREATE INDEX IF NOT EXISTS asset_price_source_policy_source_idx
  ON analytics.asset_price_source_policy (price_source, asset_id);

CREATE TABLE IF NOT EXISTS analytics.data_quality_daily (
  check_date date NOT NULL,
  check_name text NOT NULL,
  severity text NOT NULL,
  status text NOT NULL,
  observed_value double precision,
  threshold_value double precision,
  affected_rows bigint,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  checked_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (check_date, check_name),
  CONSTRAINT data_quality_severity_chk CHECK (severity IN ('info', 'warning', 'error')),
  CONSTRAINT data_quality_status_chk CHECK (status IN ('pass', 'warn', 'fail'))
);

COMMENT ON TABLE analytics.data_quality_daily IS
  'Machine-readable gates for point-in-time coverage, source consistency, and factor/report readiness.';

GRANT USAGE ON SCHEMA analytics TO finance_app, finance_ai_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON analytics.asset_price_source_policy, analytics.data_quality_daily TO finance_app;
GRANT SELECT ON analytics.asset_price_source_policy, analytics.data_quality_daily TO finance_ai_reader;

COMMIT;
