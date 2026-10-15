BEGIN;
CREATE TABLE IF NOT EXISTS analytics.research_snapshot_runs (
 snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), as_of_date date NOT NULL,
 decision_cutoff timestamptz NOT NULL, version text NOT NULL,
 input_fingerprint text NOT NULL, status text NOT NULL DEFAULT 'building',
 quality jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 completed_at timestamptz, UNIQUE(as_of_date,version,input_fingerprint)
);
CREATE TABLE IF NOT EXISTS analytics.research_snapshot_daily (
 snapshot_id uuid NOT NULL REFERENCES analytics.research_snapshot_runs(snapshot_id),
 asset_id uuid NOT NULL REFERENCES market.assets(asset_id),stock_code text NOT NULL,
 payload jsonb NOT NULL,content_sha256 text NOT NULL,
 PRIMARY KEY(snapshot_id,asset_id)
);
CREATE INDEX IF NOT EXISTS research_snapshot_stock_idx ON analytics.research_snapshot_daily(stock_code,snapshot_id);
CREATE TABLE IF NOT EXISTS analytics.forecast_models (
 model_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),model_version text NOT NULL,
 horizon_days int NOT NULL,trained_through date NOT NULL,decision_cutoff timestamptz NOT NULL,
 input_fingerprint text NOT NULL,parameters jsonb NOT NULL,validation jsonb NOT NULL,
 status text NOT NULL CHECK(status IN ('shadow','validated','approved','rejected')),
 approved_by text,approved_at timestamptz,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(model_version,horizon_days,input_fingerprint)
);
CREATE TABLE IF NOT EXISTS analytics.forecast_scenarios (
 snapshot_id uuid NOT NULL REFERENCES analytics.research_snapshot_runs(snapshot_id),
 asset_id uuid NOT NULL REFERENCES market.assets(asset_id),horizon_days int NOT NULL,
 model_id uuid REFERENCES analytics.forecast_models(model_id),scenario jsonb NOT NULL,
 PRIMARY KEY(snapshot_id,asset_id,horizon_days)
);
CREATE TABLE IF NOT EXISTS agent.watchlist_decisions (
 snapshot_id uuid NOT NULL REFERENCES analytics.research_snapshot_runs(snapshot_id),
 watchlist_id uuid NOT NULL REFERENCES agent.watchlists(watchlist_id),stock_code text NOT NULL,
 decision text NOT NULL CHECK(decision IN ('added','retained','removed','excluded')),
 rank int,reason jsonb NOT NULL,reference_price double precision,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(snapshot_id,watchlist_id,stock_code)
);
CREATE TABLE IF NOT EXISTS agent.watchlist_outcomes (
 snapshot_id uuid NOT NULL REFERENCES analytics.research_snapshot_runs(snapshot_id),
 stock_code text NOT NULL,horizon_days int NOT NULL,exit_date date,
 realized_return double precision,benchmark_return double precision,
 status text NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(snapshot_id,stock_code,horizon_days)
);
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS return_60d double precision;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_date_60d date;
ALTER TABLE factor.forward_returns ADD COLUMN IF NOT EXISTS exit_price_60d double precision;
GRANT SELECT,INSERT,UPDATE ON analytics.research_snapshot_runs,analytics.research_snapshot_daily,
 analytics.forecast_models,analytics.forecast_scenarios,agent.watchlist_decisions,agent.watchlist_outcomes TO finance_app;
GRANT SELECT ON analytics.research_snapshot_runs,analytics.research_snapshot_daily,
 analytics.forecast_models,analytics.forecast_scenarios,agent.watchlist_decisions,agent.watchlist_outcomes TO finance_ai_reader;
INSERT INTO ops.schema_migrations(migration_id,description) VALUES
 ('research_snapshot_v4','Frozen research snapshots, purged forecast baselines and persistent research watchlist')
 ON CONFLICT(migration_id) DO NOTHING;
COMMIT;
