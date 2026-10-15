BEGIN;

CREATE SCHEMA IF NOT EXISTS kg;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS kg.entity_versions (
  entity_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_id uuid NOT NULL REFERENCES kg.market_entities(entity_id) ON DELETE CASCADE,
  version_key text NOT NULL,
  valid_during daterange NOT NULL,
  public_known_during tstzrange NOT NULL,
  system_known_during tstzrange NOT NULL,
  name text,
  aliases text[] NOT NULL DEFAULT '{}'::text[],
  attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_system text NOT NULL,
  extractor_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT entity_versions_valid_nonempty_chk CHECK (NOT isempty(valid_during)),
  CONSTRAINT entity_versions_public_nonempty_chk CHECK (NOT isempty(public_known_during)),
  CONSTRAINT entity_versions_system_nonempty_chk CHECK (NOT isempty(system_known_during)),
  UNIQUE (entity_id, version_key)
);

CREATE TABLE IF NOT EXISTS kg.relations (
  relation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relation_key text NOT NULL UNIQUE,
  source_entity_id uuid NOT NULL REFERENCES kg.market_entities(entity_id) ON DELETE CASCADE,
  target_entity_id uuid NOT NULL REFERENCES kg.market_entities(entity_id) ON DELETE CASCADE,
  relation_type text NOT NULL REFERENCES kg.relation_types(relation_type),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT temporal_relation_self_chk CHECK (source_entity_id <> target_entity_id)
);

CREATE TABLE IF NOT EXISTS kg.relation_versions (
  relation_version_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  relation_id uuid NOT NULL REFERENCES kg.relations(relation_id) ON DELETE CASCADE,
  version_key text NOT NULL,
  valid_during daterange NOT NULL,
  public_known_during tstzrange NOT NULL,
  system_known_during tstzrange NOT NULL,
  direction smallint NOT NULL DEFAULT 0,
  confidence double precision NOT NULL,
  weight double precision,
  lag_days integer NOT NULL DEFAULT 0,
  horizon_days integer,
  numeric_value numeric,
  unit text,
  currency text,
  directness text,
  review_status text NOT NULL DEFAULT 'unreviewed',
  status text NOT NULL DEFAULT 'candidate',
  source_system text NOT NULL,
  extractor_version text NOT NULL,
  extraction_run_id uuid REFERENCES ai.extraction_runs(id) ON DELETE SET NULL,
  attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT relation_versions_valid_nonempty_chk CHECK (NOT isempty(valid_during)),
  CONSTRAINT relation_versions_public_nonempty_chk CHECK (NOT isempty(public_known_during)),
  CONSTRAINT relation_versions_system_nonempty_chk CHECK (NOT isempty(system_known_during)),
  CONSTRAINT relation_versions_direction_chk CHECK (direction IN (-1,0,1)),
  CONSTRAINT relation_versions_confidence_chk CHECK (confidence BETWEEN 0 AND 1),
  CONSTRAINT relation_versions_review_chk CHECK (review_status IN ('unreviewed','classified','approved','rejected','disputed')),
  CONSTRAINT relation_versions_status_chk CHECK (status IN ('candidate','active','expired','rejected','archived')),
  UNIQUE (relation_id, version_key)
);

ALTER TABLE kg.relation_versions
  ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE OR REPLACE FUNCTION kg.touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS relation_versions_touch_updated_at ON kg.relation_versions;
CREATE TRIGGER relation_versions_touch_updated_at
  BEFORE UPDATE ON kg.relation_versions
  FOR EACH ROW EXECUTE FUNCTION kg.touch_updated_at();

CREATE TABLE IF NOT EXISTS kg.relation_version_evidence (
  evidence_id bigserial PRIMARY KEY,
  relation_version_id uuid NOT NULL REFERENCES kg.relation_versions(relation_version_id) ON DELETE CASCADE,
  document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  chunk_id uuid,
  public_at timestamptz,
  system_at timestamptz NOT NULL,
  evidence_text text,
  span_start integer,
  span_end integer,
  confidence double precision,
  evidence_hash text NOT NULL,
  extraction_run_id uuid REFERENCES ai.extraction_runs(id) ON DELETE SET NULL,
  attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT temporal_evidence_confidence_chk CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  UNIQUE (relation_version_id, evidence_hash)
);

CREATE TABLE IF NOT EXISTS kg.source_coverage_daily (
  source_name text NOT NULL,
  dt date NOT NULL,
  expected_documents integer NOT NULL DEFAULT 0,
  rules_processed integer NOT NULL DEFAULT 0,
  llm_processed integer NOT NULL DEFAULT 0,
  relation_count integer NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'pending',
  extractor_version text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_name, dt),
  CONSTRAINT source_coverage_status_chk CHECK (status IN ('pending','partial','complete','failed'))
);

CREATE TABLE IF NOT EXISTS kg.temporal_backfill_partitions (
  stage text NOT NULL,
  source_name text NOT NULL,
  partition_start date NOT NULL,
  partition_end date NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  attempt_count integer NOT NULL DEFAULT 0,
  rows_read bigint NOT NULL DEFAULT 0,
  rows_written bigint NOT NULL DEFAULT 0,
  started_at timestamptz,
  finished_at timestamptz,
  error_text text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (stage, source_name, partition_start, partition_end),
  CONSTRAINT temporal_backfill_status_chk CHECK (status IN ('pending','running','complete','failed'))
);

CREATE TABLE IF NOT EXISTS kg.extraction_candidates (
  candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  task_type text NOT NULL,
  candidate_date date NOT NULL,
  priority_score double precision NOT NULL DEFAULT 0,
  ai_hardware_score double precision NOT NULL DEFAULT 0,
  source_authority_score double precision NOT NULL DEFAULT 0,
  ambiguity_score double precision NOT NULL DEFAULT 0,
  reasons text[] NOT NULL DEFAULT '{}'::text[],
  status text NOT NULL DEFAULT 'queued',
  extractor_version text NOT NULL,
  attempt_count integer NOT NULL DEFAULT 0,
  last_error text,
  claimed_at timestamptz,
  completed_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT extraction_candidates_status_chk CHECK (status IN ('queued','running','complete','failed','skipped')),
  UNIQUE (document_id, task_type, extractor_version)
);

CREATE TABLE IF NOT EXISTS kg.document_extraction_status (
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  extraction_method text NOT NULL,
  status text NOT NULL,
  event_count integer NOT NULL DEFAULT 0,
  keyword_count integer NOT NULL DEFAULT 0,
  processed_at timestamptz NOT NULL DEFAULT now(),
  source_updated_at timestamptz,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (document_id, extraction_method),
  CONSTRAINT document_extraction_status_chk CHECK (status IN ('ok','failed','skipped'))
);

ALTER TABLE analytics.graph_feature_daily
  ADD COLUMN IF NOT EXISTS coverage_status text,
  ADD COLUMN IF NOT EXISTS source_coverage_ratio double precision,
  ADD COLUMN IF NOT EXISTS rating_momentum_60d double precision,
  ADD COLUMN IF NOT EXISTS forecast_net_profit_growth double precision,
  ADD COLUMN IF NOT EXISTS forecast_eps_growth double precision,
  ADD COLUMN IF NOT EXISTS forecast_revenue_growth double precision,
  ADD COLUMN IF NOT EXISTS forecast_np_revision_90d double precision,
  ADD COLUMN IF NOT EXISTS forecast_eps_revision_90d double precision,
  ADD COLUMN IF NOT EXISTS forecast_revenue_revision_90d double precision,
  ADD COLUMN IF NOT EXISTS forward_earnings_yield double precision,
  ADD COLUMN IF NOT EXISTS forecast_dispersion_180d double precision,
  ADD COLUMN IF NOT EXISTS target_price_upside double precision,
  ADD COLUMN IF NOT EXISTS policy_event_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS demand_event_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS order_event_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS pricing_event_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS risk_event_impulse_30d double precision,
  ADD COLUMN IF NOT EXISTS event_source_diversity_30d double precision,
  ADD COLUMN IF NOT EXISTS graph_two_hop_exposure double precision,
  ADD COLUMN IF NOT EXISTS graph_centrality double precision,
  ADD COLUMN IF NOT EXISTS graph_evidence_diversity double precision,
  ADD COLUMN IF NOT EXISTS graph_relation_stability double precision;

CREATE INDEX IF NOT EXISTS extraction_candidates_queue_idx
  ON kg.extraction_candidates (status, priority_score DESC, candidate_date DESC);
CREATE INDEX IF NOT EXISTS extraction_candidates_document_idx
  ON kg.extraction_candidates (document_id, task_type);
CREATE INDEX IF NOT EXISTS document_extraction_status_method_idx
  ON kg.document_extraction_status (extraction_method,status,processed_at DESC);
CREATE INDEX IF NOT EXISTS source_coverage_daily_date_idx
  ON kg.source_coverage_daily (dt DESC, status, source_name);
CREATE INDEX IF NOT EXISTS reports_temporal_graph_idx
  ON research.reports (stock_code, report_date, available_at)
  INCLUDE (document_id,institution,rating,rating_change,industry_name)
  WHERE report_type='stock';
CREATE INDEX IF NOT EXISTS document_events_temporal_graph_idx
  ON analytics.document_events (event_date, extraction_method, document_id);

CREATE INDEX IF NOT EXISTS entity_versions_entity_valid_idx
  ON kg.entity_versions USING gist (entity_id, valid_during);
CREATE INDEX IF NOT EXISTS entity_versions_public_idx
  ON kg.entity_versions USING gist (public_known_during);
CREATE INDEX IF NOT EXISTS entity_versions_system_idx
  ON kg.entity_versions USING gist (system_known_during);
CREATE INDEX IF NOT EXISTS temporal_relations_source_idx
  ON kg.relations (source_entity_id, relation_type, target_entity_id);
CREATE INDEX IF NOT EXISTS temporal_relations_target_idx
  ON kg.relations (target_entity_id, relation_type, source_entity_id);
CREATE INDEX IF NOT EXISTS relation_versions_valid_idx
  ON kg.relation_versions USING gist (relation_id, valid_during);
CREATE INDEX IF NOT EXISTS relation_versions_public_idx
  ON kg.relation_versions USING gist (public_known_during);
CREATE INDEX IF NOT EXISTS relation_versions_system_idx
  ON kg.relation_versions USING gist (system_known_during);
CREATE INDEX IF NOT EXISTS relation_versions_status_idx
  ON kg.relation_versions (status, source_system, extractor_version);
CREATE INDEX IF NOT EXISTS relation_versions_updated_valid_idx
  ON kg.relation_versions (updated_at, lower(valid_during));
CREATE INDEX IF NOT EXISTS relation_versions_attributes_idx
  ON kg.relation_versions USING gin (attributes);
CREATE INDEX IF NOT EXISTS temporal_evidence_document_idx
  ON kg.relation_version_evidence (document_id, relation_version_id);
CREATE INDEX IF NOT EXISTS relation_versions_active_valid_public_gist_idx
  ON kg.relation_versions USING gist (valid_during, public_known_during)
  WHERE status NOT IN ('rejected', 'archived');
CREATE INDEX IF NOT EXISTS relation_versions_active_valid_system_gist_idx
  ON kg.relation_versions USING gist (valid_during, system_known_during)
  WHERE status NOT IN ('rejected', 'archived');
CREATE INDEX IF NOT EXISTS relation_versions_active_valid_lower_idx
  ON kg.relation_versions (lower(valid_during), relation_id)
  WHERE status NOT IN ('rejected', 'archived');
CREATE INDEX IF NOT EXISTS relation_versions_extractor_valid_lower_idx
  ON kg.relation_versions (extractor_version, lower(valid_during))
  WHERE status <> 'archived';
CREATE INDEX IF NOT EXISTS relation_versions_research_document_idx
  ON kg.relation_versions (((attributes ->> 'document_id')::uuid))
  WHERE extractor_version = 'temporal_research:v1';
CREATE INDEX IF NOT EXISTS relation_versions_event_id_idx
  ON kg.relation_versions (((attributes ->> 'event_id')::uuid))
  WHERE extractor_version = 'temporal_events:v1';
CREATE INDEX IF NOT EXISTS market_entities_name_trgm_idx
  ON kg.market_entities USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS market_entities_normalized_name_trgm_idx
  ON kg.market_entities USING gin (normalized_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS relation_version_evidence_best_idx
  ON kg.relation_version_evidence
    (relation_version_id, confidence DESC NULLS LAST, created_at DESC)
  INCLUDE (document_id, public_at);
CREATE INDEX IF NOT EXISTS graph_feature_daily_updated_dt_idx
  ON analytics.graph_feature_daily (updated_at, dt);

COMMENT ON TABLE kg.relations IS 'Stable relation identities. Time-varying facts live only in kg.relation_versions.';
COMMENT ON TABLE kg.relation_versions IS 'Append-only bitemporal facts: valid_during is world time; public/system_known_during are knowledge time.';
COMMENT ON COLUMN kg.relation_versions.valid_during IS 'Half-open date range [from,to) in which the fact holds in the modeled world.';
COMMENT ON COLUMN kg.relation_versions.public_known_during IS 'Half-open timestamp range in which the fact was publicly knowable.';
COMMENT ON COLUMN kg.relation_versions.system_known_during IS 'Half-open timestamp range in which this database actually had the fact available.';
COMMENT ON TABLE kg.relation_version_evidence IS 'Auditable source spans attached to one exact relation version.';
COMMENT ON TABLE kg.source_coverage_daily IS 'Daily extraction coverage. Missing edges are interpretable only when relevant source/date status is complete.';
COMMENT ON TABLE kg.document_extraction_status IS 'Idempotency ledger including successfully processed documents with zero extracted events.';
COMMENT ON TABLE kg.extraction_candidates IS 'Priority queue for bounded provider-neutral LLM extraction; queueing does not imply factual acceptance.';

CREATE OR REPLACE FUNCTION kg.temporal_edges_at(
  p_valid_at date,
  p_known_at timestamptz,
  p_knowledge_mode text DEFAULT 'public',
  p_entity_ids uuid[] DEFAULT NULL,
  p_relation_types text[] DEFAULT NULL,
  p_min_confidence double precision DEFAULT 0,
  p_limit integer DEFAULT 200
)
RETURNS TABLE (
  relation_id uuid, relation_version_id uuid, relation_key text,
  source_entity_id uuid, source_name text, source_type text,
  target_entity_id uuid, target_name text, target_type text,
  relation_type text, valid_during daterange,
  public_known_during tstzrange, system_known_during tstzrange,
  direction smallint, confidence double precision, weight double precision,
  status text, source_system text, extractor_version text, attributes jsonb
)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT r.relation_id, v.relation_version_id, r.relation_key,
         r.source_entity_id, se.name, se.entity_type,
         r.target_entity_id, te.name, te.entity_type,
         r.relation_type, v.valid_during,
         v.public_known_during, v.system_known_during,
         v.direction, v.confidence, v.weight, v.status,
         v.source_system, v.extractor_version, v.attributes
  FROM kg.relations r
  JOIN kg.relation_versions v ON v.relation_id=r.relation_id
  JOIN kg.market_entities se ON se.entity_id=r.source_entity_id
  JOIN kg.market_entities te ON te.entity_id=r.target_entity_id
  WHERE v.valid_during @> p_valid_at
    AND CASE p_knowledge_mode
          WHEN 'system' THEN v.system_known_during @> p_known_at
          ELSE v.public_known_during @> p_known_at
        END
    AND v.status NOT IN ('rejected','archived')
    AND (p_entity_ids IS NULL OR r.source_entity_id=ANY(p_entity_ids) OR r.target_entity_id=ANY(p_entity_ids))
    AND (p_relation_types IS NULL OR r.relation_type=ANY(p_relation_types))
    AND v.confidence >= p_min_confidence
  ORDER BY v.confidence DESC, r.relation_type, r.relation_key
  LIMIT greatest(1, least(coalesce(p_limit,200),2000));
$$;

CREATE OR REPLACE FUNCTION kg.temporal_edges_between(
  p_start_date date,
  p_end_date date,
  p_known_at timestamptz,
  p_knowledge_mode text DEFAULT 'public',
  p_entity_ids uuid[] DEFAULT NULL,
  p_relation_types text[] DEFAULT NULL,
  p_min_confidence double precision DEFAULT 0,
  p_limit integer DEFAULT 500
)
RETURNS SETOF kg.relation_versions
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT v.*
  FROM kg.relation_versions v
  JOIN kg.relations r ON r.relation_id=v.relation_id
  WHERE v.valid_during && daterange(p_start_date, p_end_date + 1, '[)')
    AND CASE p_knowledge_mode
          WHEN 'system' THEN v.system_known_during @> p_known_at
          ELSE v.public_known_during @> p_known_at
        END
    AND v.status NOT IN ('rejected','archived')
    AND (p_entity_ids IS NULL OR r.source_entity_id=ANY(p_entity_ids) OR r.target_entity_id=ANY(p_entity_ids))
    AND (p_relation_types IS NULL OR r.relation_type=ANY(p_relation_types))
    AND v.confidence >= p_min_confidence
  ORDER BY lower(v.valid_during), v.confidence DESC
  LIMIT greatest(1, least(coalesce(p_limit,500),5000));
$$;

INSERT INTO kg.relations (
  relation_key, source_entity_id, target_entity_id, relation_type, created_at, updated_at
)
SELECT c.claim_key, c.source_entity_id, c.target_entity_id, c.relation_type, c.created_at, c.updated_at
FROM kg.relation_claims c
WHERE c.source_entity_id <> c.target_entity_id
ON CONFLICT (relation_key) DO NOTHING;

INSERT INTO kg.relation_versions (
  relation_id, version_key, valid_during, public_known_during, system_known_during,
  direction, confidence, weight, lag_days, horizon_days, numeric_value, unit, currency,
  directness, review_status, status, source_system, extractor_version, attributes, created_at
)
SELECT r.relation_id, 'legacy:' || c.claim_id::text,
       daterange(c.valid_from, CASE WHEN c.valid_to IS NULL THEN NULL ELSE c.valid_to + 1 END, '[)'),
       tstzrange(c.known_at, c.expired_at, '[)'),
       tstzrange(greatest(c.created_at, c.known_at), NULL, '[)'),
       c.direction, c.confidence, c.weight, c.lag_days, c.horizon_days,
       c.numeric_value, c.unit, c.currency, c.directness, c.review_status, c.status,
       c.source_system, c.extractor_version,
       c.metadata || jsonb_build_object('legacy_claim_id', c.claim_id), c.created_at
FROM kg.relation_claims c
JOIN kg.relations r ON r.relation_key=c.claim_key
WHERE c.expired_at IS NULL OR c.expired_at > c.known_at
ON CONFLICT (relation_id, version_key) DO NOTHING;

INSERT INTO kg.relation_version_evidence (
  relation_version_id, document_id, chunk_id, public_at, system_at,
  evidence_text, span_start, span_end, confidence, evidence_hash,
  extraction_run_id, attributes, created_at
)
SELECT v.relation_version_id, e.document_id, e.chunk_id, e.source_published_at,
       greatest(e.created_at, coalesce(e.source_published_at, e.created_at)),
       e.evidence_text, e.span_start, e.span_end, e.confidence, e.evidence_hash,
       e.extraction_run_id, e.metadata || jsonb_build_object('legacy_evidence_id', e.evidence_id), e.created_at
FROM kg.claim_evidence e
JOIN kg.relation_versions v ON v.version_key='legacy:' || e.claim_id::text
ON CONFLICT (relation_version_id, evidence_hash) DO NOTHING;

GRANT SELECT ON kg.entity_versions, kg.relations, kg.relation_versions,
  kg.relation_version_evidence, kg.source_coverage_daily, kg.temporal_backfill_partitions,
  kg.extraction_candidates, kg.document_extraction_status
  TO finance_ai_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON kg.entity_versions, kg.relations,
  kg.relation_versions, kg.relation_version_evidence, kg.source_coverage_daily,
  kg.temporal_backfill_partitions, kg.extraction_candidates TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON kg.document_extraction_status TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA kg TO finance_app;
GRANT EXECUTE ON FUNCTION kg.temporal_edges_at(date,timestamptz,text,uuid[],text[],double precision,integer)
  TO finance_app, finance_ai_reader;
GRANT EXECUTE ON FUNCTION kg.temporal_edges_between(date,date,timestamptz,text,uuid[],text[],double precision,integer)
  TO finance_app, finance_ai_reader;

COMMIT;
