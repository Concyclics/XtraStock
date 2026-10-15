CREATE TABLE IF NOT EXISTS ops.data_asset_registry (
 dataset_key text PRIMARY KEY, title text NOT NULL, storage_kind text NOT NULL
 CHECK(storage_kind IN ('sql','files','sqlite','search_index')),
 locator text NOT NULL, grain text NOT NULL, usage_contract text NOT NULL,
 dependencies text[] NOT NULL DEFAULT '{}', registered_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS research.experiment_artifacts (
 artifact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project_key text NOT NULL,
 relative_path text NOT NULL, content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
 size_bytes bigint NOT NULL CHECK(size_bytes>=0), source_mtime_ns bigint NOT NULL,
 media_type text NOT NULL, artifact_kind text NOT NULL,
 usage_status text NOT NULL DEFAULT 'research_only' CHECK(usage_status='research_only'),
 registered_at timestamptz NOT NULL DEFAULT now(), verified_at timestamptz NOT NULL DEFAULT now(),
 record_count integer NOT NULL DEFAULT 0, payload_status text NOT NULL,
 UNIQUE(relative_path,content_sha256)
);
CREATE INDEX IF NOT EXISTS experiment_artifacts_project_idx ON research.experiment_artifacts(project_key,registered_at DESC);
CREATE TABLE IF NOT EXISTS research.artifact_records (
 artifact_id uuid REFERENCES research.experiment_artifacts ON DELETE RESTRICT,
 row_number integer NOT NULL, payload jsonb NOT NULL, PRIMARY KEY(artifact_id,row_number)
);
CREATE OR REPLACE VIEW research.latest_experiment_artifacts AS
 SELECT DISTINCT ON(relative_path) * FROM research.experiment_artifacts
 ORDER BY relative_path,registered_at DESC,artifact_id;
GRANT SELECT,INSERT,UPDATE ON ops.data_asset_registry,research.experiment_artifacts,research.artifact_records TO finance_app;
GRANT SELECT ON research.latest_experiment_artifacts TO finance_app;
