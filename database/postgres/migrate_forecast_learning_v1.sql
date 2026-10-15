BEGIN;
SET LOCAL lock_timeout='3s';
CREATE TABLE IF NOT EXISTS analytics.learning_models_v1 (
 model_id uuid PRIMARY KEY, model_version text NOT NULL, horizon integer NOT NULL CHECK(horizon IN (5,20,60)),
 as_of date NOT NULL, created_at timestamptz NOT NULL, input_digest text NOT NULL,
 knowledge_mode text NOT NULL CHECK(knowledge_mode='public_reconstruction'),
 status text NOT NULL CHECK(status='research'), input_stats jsonb NOT NULL,
 parameters jsonb NOT NULL, evaluation jsonb NOT NULL, dataset_path text NOT NULL,
 UNIQUE(model_version,horizon,input_digest)
);
CREATE TABLE IF NOT EXISTS analytics.learning_cycles_v1 (
 cycle_id uuid PRIMARY KEY, started_at timestamptz NOT NULL, completed_at timestamptz NOT NULL,
 as_of date NOT NULL, status text NOT NULL, summary jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS learning_cycles_recent_idx ON analytics.learning_cycles_v1(completed_at DESC);
CREATE TABLE IF NOT EXISTS analytics.learning_predictions_v1 (
 prediction_id uuid PRIMARY KEY, model_id uuid NOT NULL REFERENCES analytics.learning_models_v1,
 stock_code text NOT NULL, name text NOT NULL, signal_date date NOT NULL, horizon integer NOT NULL,
 entry_date date NOT NULL, exit_date date NOT NULL, created_at timestamptz NOT NULL,
 knowledge_mode text NOT NULL CHECK(knowledge_mode='system_forward'),
 expected_gross_return double precision NOT NULL, interval_80 jsonb NOT NULL,
 momentum_baseline double precision NOT NULL, research_qualified boolean NOT NULL,
 frozen_input jsonb NOT NULL, UNIQUE(model_id,stock_code,signal_date,horizon),
 CHECK(entry_date>signal_date AND exit_date>entry_date)
);
CREATE INDEX IF NOT EXISTS learning_predictions_latest_idx ON analytics.learning_predictions_v1(signal_date DESC,stock_code,horizon);
CREATE TABLE IF NOT EXISTS analytics.learning_outcomes_v1 (
 outcome_id uuid PRIMARY KEY, prediction_id uuid NOT NULL REFERENCES analytics.learning_predictions_v1,
 observed_at timestamptz NOT NULL, source_digest text NOT NULL, result jsonb NOT NULL,
 UNIQUE(prediction_id,source_digest)
);
CREATE INDEX IF NOT EXISTS learning_outcomes_latest_idx ON analytics.learning_outcomes_v1(prediction_id,observed_at DESC);
COMMENT ON TABLE analytics.learning_models_v1 IS 'Append-only small-stock research models. Reconstructed history, never automatic production approval.';
COMMENT ON TABLE analytics.learning_outcomes_v1 IS 'Append-only realized outcome revisions for frozen prospective predictions; daily-price proxy, not executions.';
GRANT SELECT,INSERT ON analytics.learning_models_v1,analytics.learning_cycles_v1,analytics.learning_predictions_v1,analytics.learning_outcomes_v1 TO finance_app;
GRANT SELECT ON analytics.learning_models_v1,analytics.learning_cycles_v1,analytics.learning_predictions_v1,analytics.learning_outcomes_v1 TO finance_ai_reader;
COMMIT;
