-- Additive, separately versioned data and paper-plan ledger. No broker orders.
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE TABLE IF NOT EXISTS research.earnings_observations (
 observation_id text PRIMARY KEY,
 stock_code text NOT NULL, institution text NOT NULL, document_id uuid REFERENCES corpus.documents(id),
 source_identifier text NOT NULL, report_date date NOT NULL,
 forecast_year integer NOT NULL CHECK(forecast_year BETWEEN 2000 AND 2100),
 metric text NOT NULL CHECK(metric IN('eps','net_profit','pe')),
 value numeric NOT NULL, unit text NOT NULL,
 source_basis text NOT NULL CHECK(source_basis IN('explicit_dated_report_text','observed_api_current_year')),
 known_at timestamptz NOT NULL, observed_at timestamptz NOT NULL,
 evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 CHECK((metric='eps' AND unit='CNY/share') OR (metric='net_profit' AND unit='CNY') OR (metric='pe' AND unit='times')),
 CHECK(known_at>=observed_at OR source_basis='explicit_dated_report_text')
);
CREATE INDEX IF NOT EXISTS earnings_observations_scope ON research.earnings_observations(stock_code,forecast_year,known_at);
CREATE TABLE IF NOT EXISTS research.earnings_extraction_receipts (
 document_id uuid NOT NULL REFERENCES corpus.documents(id), parser_version text NOT NULL,
 content_digest text NOT NULL, observed_at timestamptz NOT NULL, observations integer NOT NULL,
 issues jsonb NOT NULL, PRIMARY KEY(document_id,parser_version,content_digest)
);
CREATE TABLE IF NOT EXISTS quant.daily_plan_runs (
 run_id text PRIMARY KEY, strategy_version text NOT NULL, report_date date NOT NULL,
 decision_at timestamptz NOT NULL, evidence_cutoff timestamptz NOT NULL,
 market_date date NOT NULL, report_run_id uuid, mode text NOT NULL CHECK(mode IN('shadow','approved')),
 config_digest text NOT NULL, source_digest text NOT NULL, validation jsonb NOT NULL,
 summary jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(strategy_version,report_date), CHECK(evidence_cutoff<=decision_at)
);
CREATE TABLE IF NOT EXISTS quant.daily_plan_revisions (
 plan_id text PRIMARY KEY, run_id text NOT NULL REFERENCES quant.daily_plan_runs(run_id),
 stock_code text NOT NULL, previous_plan_id text REFERENCES quant.daily_plan_revisions(plan_id),
 state text NOT NULL CHECK(state IN('observe','eligible_to_buy','pending_limit_entry','held','reduce','exit','removed','data_review')),
 payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(run_id,stock_code)
);
CREATE INDEX IF NOT EXISTS daily_plan_history ON quant.daily_plan_revisions(stock_code,created_at);
CREATE TABLE IF NOT EXISTS quant.daily_plan_events (
 event_id text PRIMARY KEY, plan_id text NOT NULL REFERENCES quant.daily_plan_revisions(plan_id),
 event_type text NOT NULL CHECK(event_type IN('order','fill','partial','expired','cancelled','mark','exit','review')),
 event_at timestamptz NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS quant.daily_plan_deliveries (
 run_id text NOT NULL REFERENCES quant.daily_plan_runs(run_id), target_key text NOT NULL,
 part integer NOT NULL, status text NOT NULL CHECK(status IN('pending','sending','sent','failed','uncertain')),
 payload jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(run_id,target_key,part)
);
CREATE TABLE IF NOT EXISTS quant.daily_plan_accounts (
 account_id text PRIMARY KEY, initial_cash numeric NOT NULL, state jsonb NOT NULL,
 policy jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS quant.daily_plan_sessions (
 account_id text NOT NULL REFERENCES quant.daily_plan_accounts(account_id), session_date date NOT NULL,
 result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(account_id,session_date)
);
CREATE OR REPLACE FUNCTION quant.reject_plan_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Immutable evidence/plan ledger; append a revision or event'; END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['daily_plan_runs','daily_plan_revisions','daily_plan_events','daily_plan_sessions'] LOOP
   IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname=t||'_immutable' AND tgrelid=('quant.'||t)::regclass) THEN
     EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON quant.%I FOR EACH ROW EXECUTE FUNCTION quant.reject_plan_rewrite()',t||'_immutable',t);
   END IF;
 END LOOP;
 IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgname='earnings_observations_immutable' AND tgrelid='research.earnings_observations'::regclass) THEN
  CREATE TRIGGER earnings_observations_immutable BEFORE UPDATE OR DELETE ON research.earnings_observations
  FOR EACH ROW EXECUTE FUNCTION quant.reject_plan_rewrite();
 END IF;
END $$;
-- Preserve existing schema role conventions; never broaden login/network access.
DO $$ DECLARE role_name text; BEGIN
 FOREACH role_name IN ARRAY ARRAY['finance_app','finance_ai_reader','qieting'] LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
   EXECUTE format('GRANT SELECT ON research.earnings_observations,research.earnings_extraction_receipts,quant.daily_plan_runs,quant.daily_plan_revisions,quant.daily_plan_events,quant.daily_plan_deliveries,quant.daily_plan_accounts,quant.daily_plan_sessions TO %I',role_name);
  END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='finance_app') THEN
  GRANT INSERT ON research.earnings_observations,research.earnings_extraction_receipts,quant.daily_plan_runs,quant.daily_plan_revisions,quant.daily_plan_events TO finance_app;
  GRANT INSERT,UPDATE ON quant.daily_plan_deliveries TO finance_app;
  GRANT INSERT,UPDATE ON quant.daily_plan_accounts TO finance_app;
  GRANT INSERT ON quant.daily_plan_sessions TO finance_app;
 END IF;
END $$;
COMMIT;
