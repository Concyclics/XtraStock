\echo 'Initializing finance_corpus extensions'

DO $$
DECLARE
  ext_name text;
  ext_names text[] := ARRAY[
    'timescaledb',
    'timescaledb_toolkit',
    'vector',
    'vectorscale',
    'pg_stat_statements',
    'pg_trgm',
    'btree_gin',
    'btree_gist'
  ];
BEGIN
  FOREACH ext_name IN ARRAY ext_names LOOP
    IF EXISTS (
      SELECT 1
      FROM pg_available_extensions
      WHERE name = ext_name
    ) THEN
      EXECUTE format('CREATE EXTENSION IF NOT EXISTS %I', ext_name);
      RAISE NOTICE 'extension enabled: %', ext_name;
    ELSE
      RAISE NOTICE 'extension unavailable, skipped: %', ext_name;
    END IF;
  END LOOP;
END
$$;

CREATE SCHEMA IF NOT EXISTS corpus;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS analytics;

COMMENT ON SCHEMA corpus IS 'Unified document corpus: reports, OCR text, news, and chunks.';
COMMENT ON SCHEMA market IS 'Market data and other time-series hypertables.';
COMMENT ON SCHEMA analytics IS 'Derived metrics, continuous aggregates, and agent analysis outputs.';
