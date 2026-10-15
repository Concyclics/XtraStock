BEGIN;
SET LOCAL lock_timeout='5s';
CREATE TABLE IF NOT EXISTS quant.repair_queue(
 repair_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 pipeline text NOT NULL CHECK(pipeline IN('knowledge_graph','factor_daily')),
 since_date date NOT NULL,until_date date NOT NULL CHECK(until_date>=since_date),
 source_cutoff timestamptz NOT NULL,status text NOT NULL DEFAULT 'pending' CHECK(status IN('pending','complete')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),completed_at timestamptz,
 UNIQUE(pipeline,since_date,until_date,source_cutoff));
ALTER TABLE agent.watchlist_outcomes ADD COLUMN IF NOT EXISTS return_basis text NOT NULL DEFAULT 'reference_close_diagnostic';
ALTER TABLE agent.watchlist_outcomes ADD COLUMN IF NOT EXISTS executable_result boolean NOT NULL DEFAULT false;
GRANT SELECT,INSERT,UPDATE ON quant.repair_queue TO finance_app;
GRANT USAGE,SELECT ON SEQUENCE quant.repair_queue_repair_id_seq TO finance_app;
GRANT SELECT ON quant.repair_queue TO finance_ai_reader;
INSERT INTO ops.schema_migrations(migration_id,description) VALUES
 ('quant_repair_queue_v1','Retain old corrections outside the daily budget; label reference-close diagnostics') ON CONFLICT DO NOTHING;
COMMIT;
