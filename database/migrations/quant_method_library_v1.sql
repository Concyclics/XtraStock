BEGIN;
CREATE TABLE IF NOT EXISTS research.quant_method_versions (
  method_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  method_key text NOT NULL,
  title text NOT NULL,
  source_url text NOT NULL CHECK (source_url LIKE 'https://%'),
  content_sha256 text NOT NULL CHECK (length(content_sha256)=64),
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
  registered_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(method_key, content_sha256)
);
CREATE INDEX IF NOT EXISTS quant_method_versions_time_idx
  ON research.quant_method_versions(method_key,registered_at DESC);
GRANT SELECT, INSERT ON research.quant_method_versions TO finance_app;
GRANT SELECT ON research.quant_method_versions TO finance_ai_reader;
COMMIT;
