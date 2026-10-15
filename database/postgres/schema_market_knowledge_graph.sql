BEGIN;

CREATE SCHEMA IF NOT EXISTS kg;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS kg.market_entities (
  entity_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type text NOT NULL,
  canonical_key text NOT NULL UNIQUE,
  name text NOT NULL,
  normalized_name text NOT NULL,
  aliases text[] NOT NULL DEFAULT '{}'::text[],
  source_ref text,
  status text NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT kg_entities_status_chk CHECK (status IN ('candidate', 'active', 'inactive', 'archived'))
);

COMMENT ON TABLE kg.market_entities IS 'General full-market knowledge-graph entities: companies, industries, institutions, concepts, events, and macro indicators.';
CREATE INDEX IF NOT EXISTS kg_entities_type_name_idx ON kg.market_entities (entity_type, normalized_name);
CREATE INDEX IF NOT EXISTS kg_entities_source_ref_idx ON kg.market_entities (entity_type, source_ref);
CREATE INDEX IF NOT EXISTS kg_entities_aliases_idx ON kg.market_entities USING gin (aliases);
CREATE INDEX IF NOT EXISTS kg_entities_metadata_idx ON kg.market_entities USING gin (metadata);

CREATE TABLE IF NOT EXISTS kg.market_entity_relations (
  relation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relation_key text NOT NULL UNIQUE,
  source_entity_id uuid NOT NULL REFERENCES kg.market_entities(entity_id) ON DELETE CASCADE,
  target_entity_id uuid NOT NULL REFERENCES kg.market_entities(entity_id) ON DELETE CASCADE,
  relation_type text NOT NULL,
  valid_from date NOT NULL,
  valid_to date,
  evidence_date date,
  observed_at timestamptz NOT NULL DEFAULT now(),
  confidence double precision,
  weight double precision,
  evidence_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  evidence text,
  source_system text NOT NULL,
  extractor_version text,
  status text NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT kg_entity_relations_dates_chk CHECK (valid_to IS NULL OR valid_to >= valid_from),
  CONSTRAINT kg_entity_relations_confidence_chk CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  CONSTRAINT kg_entity_relations_status_chk CHECK (status IN ('candidate', 'active', 'expired', 'rejected', 'archived'))
);

COMMENT ON TABLE kg.market_entity_relations IS 'Evidence-backed temporal relationships. Point events use valid_from=valid_to; current classifications keep valid_to NULL until superseded.';
CREATE INDEX IF NOT EXISTS kg_entity_rel_source_idx ON kg.market_entity_relations (source_entity_id, relation_type, valid_from DESC);
CREATE INDEX IF NOT EXISTS kg_entity_rel_target_idx ON kg.market_entity_relations (target_entity_id, relation_type, valid_from DESC);
CREATE INDEX IF NOT EXISTS kg_entity_rel_active_idx ON kg.market_entity_relations (relation_type, valid_from DESC, valid_to) WHERE status='active';
CREATE INDEX IF NOT EXISTS kg_entity_rel_evidence_idx ON kg.market_entity_relations (evidence_document_id) WHERE evidence_document_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS kg_entity_rel_metadata_idx ON kg.market_entity_relations USING gin (metadata);

CREATE TABLE IF NOT EXISTS analytics.kg_relation_daily_stats (
  as_of_date date NOT NULL,
  relation_type text NOT NULL,
  source_entity_type text NOT NULL,
  target_entity_type text NOT NULL,
  relation_count bigint NOT NULL,
  distinct_source_count bigint NOT NULL,
  distinct_target_count bigint NOT NULL,
  avg_confidence double precision,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (as_of_date, relation_type, source_entity_type, target_entity_type)
);

CREATE INDEX IF NOT EXISTS kg_relation_daily_stats_date_idx
  ON analytics.kg_relation_daily_stats (as_of_date DESC, relation_count DESC);

GRANT USAGE ON SCHEMA kg, analytics TO finance_app, finance_ai_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON kg.market_entities, kg.market_entity_relations, analytics.kg_relation_daily_stats TO finance_app;
GRANT SELECT ON kg.market_entities, kg.market_entity_relations, analytics.kg_relation_daily_stats TO finance_ai_reader;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA kg TO finance_app;

COMMIT;
