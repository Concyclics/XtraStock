\echo 'Applying temporal graph and factor query indexes'

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Point-in-time graph materialization filters world time and knowledge time
-- together. Partial indexes exclude versions that can never be returned.
CREATE INDEX CONCURRENTLY IF NOT EXISTS relation_versions_active_valid_public_gist_idx
  ON kg.relation_versions USING gist (valid_during, public_known_during)
  WHERE status NOT IN ('rejected', 'archived');

CREATE INDEX CONCURRENTLY IF NOT EXISTS relation_versions_active_valid_system_gist_idx
  ON kg.relation_versions USING gist (valid_during, system_known_during)
  WHERE status NOT IN ('rejected', 'archived');

-- Recent/changes modes and the daily feature builder use lower(valid_during),
-- which the range GiST indexes do not accelerate efficiently.
CREATE INDEX CONCURRENTLY IF NOT EXISTS relation_versions_active_valid_lower_idx
  ON kg.relation_versions (lower(valid_during), relation_id)
  WHERE status NOT IN ('rejected', 'archived');

-- Monthly rebuilds archive one extractor/date slice before recreating it.
CREATE INDEX CONCURRENTLY IF NOT EXISTS relation_versions_extractor_valid_lower_idx
  ON kg.relation_versions (extractor_version, lower(valid_during))
  WHERE status <> 'archived';

-- Evidence rebuilds join identifiers stored in the version payload. These
-- partial expression indexes stay small and match the owning extractor.
CREATE INDEX CONCURRENTLY IF NOT EXISTS relation_versions_research_document_idx
  ON kg.relation_versions (((attributes ->> 'document_id')::uuid))
  WHERE extractor_version = 'temporal_research:v1';

CREATE INDEX CONCURRENTLY IF NOT EXISTS relation_versions_event_id_idx
  ON kg.relation_versions (((attributes ->> 'event_id')::uuid))
  WHERE extractor_version = 'temporal_events:v1';

-- The Agent supports contains-style entity lookup in addition to exact aliases.
CREATE INDEX CONCURRENTLY IF NOT EXISTS market_entities_name_trgm_idx
  ON kg.market_entities USING gin (name gin_trgm_ops);

CREATE INDEX CONCURRENTLY IF NOT EXISTS market_entities_normalized_name_trgm_idx
  ON kg.market_entities USING gin (normalized_name gin_trgm_ops);

-- LATERAL evidence lookup asks for the strongest, newest evidence per version.
CREATE INDEX CONCURRENTLY IF NOT EXISTS relation_version_evidence_best_idx
  ON kg.relation_version_evidence
    (relation_version_id, confidence DESC NULLS LAST, created_at DESC)
  INCLUDE (document_id, public_at);

ANALYZE kg.relation_versions;
ANALYZE kg.relation_version_evidence;
ANALYZE kg.market_entities;
