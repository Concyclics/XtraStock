BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='20s';
CREATE TABLE IF NOT EXISTS analytics.research_focus_runs (
 run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 version text NOT NULL,as_of_date date NOT NULL,decision_cutoff timestamptz NOT NULL,
 input_fingerprint text NOT NULL,source_snapshot_id uuid REFERENCES analytics.research_snapshot_runs(snapshot_id),
 summary jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(version,input_fingerprint)
);
CREATE INDEX IF NOT EXISTS research_focus_runs_cutoff_idx
 ON analytics.research_focus_runs(created_at DESC,decision_cutoff DESC);
CREATE TABLE IF NOT EXISTS analytics.research_focus_revisions (
 revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),run_id uuid NOT NULL REFERENCES analytics.research_focus_runs(run_id),
 focus_key text NOT NULL,entity_type text NOT NULL CHECK(entity_type IN ('stock','theme')),
 label text NOT NULL,stock_codes text[] NOT NULL,state text NOT NULL CHECK(state IN ('active','watching','cooling','archived')),
 priority double precision NOT NULL CHECK(priority>=0 AND priority<=100),
 next_review_at timestamptz NOT NULL,payload jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(run_id,focus_key)
);
CREATE INDEX IF NOT EXISTS research_focus_revisions_run_rank_idx
 ON analytics.research_focus_revisions(run_id,state,priority DESC);
CREATE INDEX IF NOT EXISTS research_focus_revisions_codes_idx
 ON analytics.research_focus_revisions USING gin(stock_codes);
COMMENT ON TABLE analytics.research_focus_revisions IS
 'Append-only evidence-based research attention, not a stock recommendation or forecast approval. Candidate/rejected graph relations excluded; source reports and extracted leads remain explicitly distinct from verified observations.';
REVOKE UPDATE,DELETE,TRUNCATE ON analytics.research_focus_runs,analytics.research_focus_revisions FROM finance_app,finance_ai_reader;
GRANT SELECT,INSERT ON analytics.research_focus_runs,analytics.research_focus_revisions TO finance_app;
GRANT SELECT ON analytics.research_focus_runs,analytics.research_focus_revisions TO finance_ai_reader;
INSERT INTO ops.schema_migrations(migration_id,description)
 VALUES('research_focus_v1','Immutable evidence-driven research attention with deduplication, hysteresis and follow-up checks')
 ON CONFLICT(migration_id) DO NOTHING;
COMMIT;
