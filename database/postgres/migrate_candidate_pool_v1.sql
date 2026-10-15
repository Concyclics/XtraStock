BEGIN;
SET LOCAL lock_timeout='3s';
CREATE TABLE IF NOT EXISTS analytics.candidate_pool_runs (
 run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 version text NOT NULL, as_of_date date NOT NULL, decision_cutoff timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 input_fingerprint text NOT NULL UNIQUE, source_snapshot_id uuid NOT NULL,
 weekly_key text NOT NULL, monthly_key text NOT NULL, summary jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS candidate_pool_runs_latest ON analytics.candidate_pool_runs(created_at DESC);
CREATE TABLE IF NOT EXISTS analytics.candidate_pool_revisions (
 run_id uuid NOT NULL REFERENCES analytics.candidate_pool_runs(run_id),
 stock_code text NOT NULL, state text NOT NULL CHECK(state IN ('core','watching','exploring','cooling','archived')),
 rank integer, score double precision, payload jsonb NOT NULL,
 PRIMARY KEY(run_id,stock_code)
);
CREATE INDEX IF NOT EXISTS candidate_pool_stock_history ON analytics.candidate_pool_revisions(stock_code,run_id);
CREATE TABLE IF NOT EXISTS analytics.factor_reconstruction_runs (
 run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 as_of_date date NOT NULL,input_fingerprint text NOT NULL UNIQUE,config jsonb NOT NULL,summary jsonb NOT NULL
);
GRANT SELECT,INSERT ON analytics.candidate_pool_runs,analytics.candidate_pool_revisions,analytics.factor_reconstruction_runs TO finance_app;
GRANT SELECT ON analytics.candidate_pool_runs,analytics.candidate_pool_revisions,analytics.factor_reconstruction_runs TO finance_ai_reader;
REVOKE UPDATE,DELETE,TRUNCATE ON analytics.candidate_pool_runs,analytics.candidate_pool_revisions,analytics.factor_reconstruction_runs FROM finance_app,finance_ai_reader;
COMMIT;
