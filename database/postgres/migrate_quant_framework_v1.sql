BEGIN;
SET LOCAL lock_timeout='5s';
CREATE SCHEMA IF NOT EXISTS quant;
CREATE TABLE IF NOT EXISTS quant.readiness_runs(
 readiness_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 as_of_date date NOT NULL, checked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 contract_version text NOT NULL, status text NOT NULL CHECK(status IN('ready','blocked')),
 checks jsonb NOT NULL, evidence jsonb NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS quant_readiness_latest ON quant.readiness_runs(as_of_date,checked_at DESC);
CREATE TABLE IF NOT EXISTS quant.paper_accounts(
 account_id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 initial_cash numeric NOT NULL CHECK(initial_cash>0), policy jsonb NOT NULL,
 state jsonb NOT NULL, strategy_version text NOT NULL,
 mode text NOT NULL DEFAULT 'paper' CHECK(mode='paper'));
CREATE TABLE IF NOT EXISTS quant.paper_sessions(
 account_id text NOT NULL REFERENCES quant.paper_accounts(account_id),session_date date NOT NULL,
 status text NOT NULL CHECK(status IN('complete','blocked')), readiness_id uuid REFERENCES quant.readiness_runs,
 payload jsonb NOT NULL, completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(account_id,session_date));
CREATE TABLE IF NOT EXISTS quant.paper_orders(
 order_id text PRIMARY KEY,account_id text NOT NULL REFERENCES quant.paper_accounts,
 snapshot_id uuid NOT NULL REFERENCES analytics.research_snapshot_runs,
 decision_at timestamptz NOT NULL,session_date date NOT NULL, stock_code text NOT NULL,
 side text NOT NULL CHECK(side IN('buy','sell')),quantity int NOT NULL CHECK(quantity>0),
 status text NOT NULL DEFAULT 'pending',filled_quantity int NOT NULL DEFAULT 0, reason text,
 UNIQUE(account_id,snapshot_id,stock_code,side));
CREATE TABLE IF NOT EXISTS quant.paper_fills(
 order_id text PRIMARY KEY REFERENCES quant.paper_orders,session_date date NOT NULL,
 quantity int NOT NULL CHECK(quantity>0),price numeric NOT NULL CHECK(price>0),fees numeric NOT NULL,
 gross numeric NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS quant.paper_nav(
 account_id text NOT NULL REFERENCES quant.paper_accounts,session_date date NOT NULL,
 cash numeric NOT NULL,receivable numeric NOT NULL,nav numeric NOT NULL,turnover numeric NOT NULL,
 positions jsonb NOT NULL, benchmark_close numeric,benchmark_name text,
 PRIMARY KEY(account_id,session_date));
CREATE TABLE IF NOT EXISTS quant.research_contracts(
 contract_id text PRIMARY KEY, version text NOT NULL, definition jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp());
INSERT INTO quant.research_contracts(contract_id,version,definition) VALUES
 ('paper-mainboard-weekly','paper-ledger-v1','{"mode":"paper","rebalance":"weekly","signal":"equal weighted rank of price baseline factors","universe":"verified main-board securities only","price_basis":"unadjusted execution prices","forecast_horizons":[20,60],"promotion":"no automatic production promotion","costs":"explicit research assumptions; not broker tariff"}')
 ON CONFLICT(contract_id) DO NOTHING;
GRANT USAGE ON SCHEMA quant TO finance_app,finance_ai_reader;
GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA quant TO finance_app;
GRANT SELECT ON ALL TABLES IN SCHEMA quant TO finance_ai_reader;
INSERT INTO ops.schema_migrations(migration_id,description) VALUES
 ('quant_framework_v1','Explicit research readiness and paper-only auditable ledger') ON CONFLICT DO NOTHING;
COMMIT;
