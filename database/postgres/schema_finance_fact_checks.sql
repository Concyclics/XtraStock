BEGIN;

CREATE SCHEMA IF NOT EXISTS company;

CREATE TABLE IF NOT EXISTS company.financial_statement_metrics (
  id bigserial PRIMARY KEY,
  asset_id uuid REFERENCES market.assets(asset_id) ON DELETE SET NULL,
  stock_code text NOT NULL,
  stock_name text,
  report_period text NOT NULL,
  report_type text NOT NULL DEFAULT 'quarterly',
  report_date date,
  metric_name text NOT NULL,
  metric_value numeric,
  metric_yoy double precision,
  source_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  source_text text,
  confidence numeric,
  extraction_method text NOT NULL DEFAULT 'research_report_regex',
  source text NOT NULL DEFAULT 'research.reports',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT financial_statement_metrics_report_type_chk
    CHECK (report_type IN ('quarterly', 'annual', 'ttm', 'unknown')),
  CONSTRAINT financial_statement_metrics_metric_chk
    CHECK (metric_name IN ('revenue', 'net_profit_parent', 'deducted_net_profit'))
);

COMMENT ON TABLE company.financial_statement_metrics IS
  'Actual reported company financial metrics extracted from filings/research text. These are not research forecast rows.';
COMMENT ON COLUMN company.financial_statement_metrics.report_period IS
  'Canonical period such as 2026Q1 or 2025FY.';
COMMENT ON COLUMN company.financial_statement_metrics.metric_yoy IS
  'Actual year-over-year growth as a decimal. Example: 0.5 means +50%, -0.55 means -55%.';
COMMENT ON COLUMN company.financial_statement_metrics.source_text IS
  'Short source sentence used to extract the metric; expose this to agents as evidence.';

CREATE UNIQUE INDEX IF NOT EXISTS financial_statement_metrics_identity_uniq
  ON company.financial_statement_metrics (
    stock_code,
    report_period,
    metric_name,
    COALESCE(source_document_id, '00000000-0000-0000-0000-000000000000'::uuid)
  );
CREATE INDEX IF NOT EXISTS financial_statement_metrics_period_metric_yoy_idx
  ON company.financial_statement_metrics (report_period, metric_name, metric_yoy DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS financial_statement_metrics_stock_period_idx
  ON company.financial_statement_metrics (stock_code, report_period);
CREATE INDEX IF NOT EXISTS financial_statement_metrics_document_idx
  ON company.financial_statement_metrics (source_document_id);

CREATE TABLE IF NOT EXISTS analytics.asset_event_return (
  id bigserial PRIMARY KEY,
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  stock_code text NOT NULL,
  stock_name text,
  event_type text NOT NULL,
  event_date date NOT NULL,
  base_trade_date date,
  latest_trade_date date,
  base_close double precision,
  latest_close double precision,
  return_pct double precision,
  price_source text NOT NULL DEFAULT 'eastmoney:kline:day:qfq',
  adjustment text NOT NULL DEFAULT 'qfq',
  adjustment_fallback boolean NOT NULL DEFAULT false,
  source_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  data_quality text NOT NULL DEFAULT 'ok',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT asset_event_return_quality_chk CHECK (
    data_quality IN ('ok', 'missing_price', 'mixed_adjustment_rejected', 'low_confidence_extraction')
  )
);

COMMENT ON TABLE analytics.asset_event_return IS
  'Agent-facing event-date to latest-date stock return facts with one explicit price adjustment/source per row.';
COMMENT ON COLUMN analytics.asset_event_return.return_pct IS
  'Return as decimal from base_trade_date close to latest_trade_date close.';
COMMENT ON COLUMN analytics.asset_event_return.price_source IS
  'Exact market.bars_daily source used for both base and latest closes.';

CREATE UNIQUE INDEX IF NOT EXISTS asset_event_return_identity_uniq
  ON analytics.asset_event_return (
    stock_code,
    event_type,
    event_date,
    COALESCE(source_document_id, '00000000-0000-0000-0000-000000000000'::uuid),
    price_source
  );
CREATE INDEX IF NOT EXISTS asset_event_return_type_date_return_idx
  ON analytics.asset_event_return (event_type, event_date DESC, return_pct);
CREATE INDEX IF NOT EXISTS asset_event_return_stock_date_idx
  ON analytics.asset_event_return (stock_code, event_date DESC);

CREATE INDEX IF NOT EXISTS bars_daily_asset_source_ts_desc_idx
  ON market.bars_daily (asset_id, source, ts DESC);
CREATE INDEX IF NOT EXISTS bars_daily_asset_source_ts_idx
  ON market.bars_daily (asset_id, source, ts);

CREATE INDEX IF NOT EXISTS reports_stock_report_date_idx
  ON research.reports (stock_code, report_date DESC);
CREATE INDEX IF NOT EXISTS reports_document_id_idx
  ON research.reports (document_id);
CREATE INDEX IF NOT EXISTS report_forecasts_document_idx
  ON research.report_forecasts (document_id);

GRANT USAGE ON SCHEMA company TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA company TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA company TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON analytics.asset_event_return TO finance_app;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE analytics.asset_event_return_id_seq TO finance_app;
GRANT USAGE ON SCHEMA company TO finance_ai_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA company TO finance_ai_reader;
GRANT SELECT ON analytics.asset_event_return TO finance_ai_reader;

COMMIT;
