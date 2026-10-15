BEGIN;

CREATE SCHEMA IF NOT EXISTS kg;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS kg.concepts (
  concept_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  concept_type text NOT NULL,
  name text NOT NULL,
  normalized_name text NOT NULL,
  aliases text[] NOT NULL DEFAULT '{}'::text[],
  description text,
  ai_hardware_relevance double precision NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'active',
  source text NOT NULL DEFAULT 'manual',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT concepts_type_chk CHECK (
    concept_type IN ('root', 'segment', 'subsegment', 'material', 'equipment', 'component', 'system', 'process', 'application', 'risk', 'metric')
  ),
  CONSTRAINT concepts_status_chk CHECK (status IN ('candidate', 'active', 'rejected', 'archived'))
);

COMMENT ON TABLE kg.concepts IS 'Knowledge-graph concepts for AI hardware industry chain: segments, subsegments, materials, equipment, components, processes, and risks.';
CREATE UNIQUE INDEX IF NOT EXISTS concepts_type_normalized_uniq ON kg.concepts (concept_type, normalized_name);
CREATE INDEX IF NOT EXISTS concepts_name_idx ON kg.concepts (name);
CREATE INDEX IF NOT EXISTS concepts_aliases_idx ON kg.concepts USING gin (aliases);
CREATE INDEX IF NOT EXISTS concepts_relevance_idx ON kg.concepts (ai_hardware_relevance DESC);

CREATE TABLE IF NOT EXISTS kg.concept_edges (
  edge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_concept_id uuid NOT NULL REFERENCES kg.concepts(concept_id) ON DELETE CASCADE,
  target_concept_id uuid NOT NULL REFERENCES kg.concepts(concept_id) ON DELETE CASCADE,
  relation_type text NOT NULL,
  confidence double precision,
  evidence_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  evidence text,
  source text NOT NULL DEFAULT 'manual',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT concept_edges_relation_chk CHECK (
    relation_type IN ('contains', 'upstream_of', 'downstream_of', 'used_in', 'equipment_for', 'material_for', 'component_of', 'benefits_from', 'risk_to', 'related_to')
  )
);

COMMENT ON TABLE kg.concept_edges IS 'Directed relationships between concepts, such as AI hardware contains optical interconnect, or tungsten hexafluoride material_for semiconductor manufacturing.';
CREATE UNIQUE INDEX IF NOT EXISTS concept_edges_identity_uniq
  ON kg.concept_edges (source_concept_id, target_concept_id, relation_type);
CREATE INDEX IF NOT EXISTS concept_edges_source_idx ON kg.concept_edges (source_concept_id, relation_type);
CREATE INDEX IF NOT EXISTS concept_edges_target_idx ON kg.concept_edges (target_concept_id, relation_type);

CREATE TABLE IF NOT EXISTS kg.company_concept_exposures (
  exposure_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid REFERENCES market.assets(asset_id) ON DELETE SET NULL,
  stock_code text NOT NULL,
  stock_name text,
  concept_id uuid NOT NULL REFERENCES kg.concepts(concept_id) ON DELETE CASCADE,
  relation_type text NOT NULL,
  evidence_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  evidence_date date,
  evidence text,
  confidence double precision,
  source text NOT NULL DEFAULT 'manual',
  status text NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  business_role text,
  directness text NOT NULL DEFAULT 'unknown',
  evidence_span text,
  extractor_version text,
  review_status text NOT NULL DEFAULT 'unreviewed',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT company_concept_relation_chk CHECK (
    relation_type IN ('direct_product', 'material_supplier', 'equipment_supplier', 'downstream_customer', 'weak_mention')
  ),
  CONSTRAINT company_concept_status_chk CHECK (status IN ('candidate', 'active', 'rejected', 'archived')),
  CONSTRAINT company_concept_directness_chk CHECK (directness IN ('direct', 'indirect', 'unknown'))
);

COMMENT ON TABLE kg.company_concept_exposures IS 'Company exposure relationships to concepts. Only direct_product, material_supplier, and equipment_supplier should drive main AI hardware rankings.';
CREATE UNIQUE INDEX IF NOT EXISTS company_concept_exposures_identity_uniq
  ON kg.company_concept_exposures (
    stock_code,
    concept_id,
    relation_type,
    COALESCE(evidence_document_id, '00000000-0000-0000-0000-000000000000'::uuid)
  );
CREATE INDEX IF NOT EXISTS company_concept_exposures_stock_idx ON kg.company_concept_exposures (stock_code, status);
CREATE INDEX IF NOT EXISTS company_concept_exposures_concept_idx ON kg.company_concept_exposures (concept_id, relation_type, status);
CREATE INDEX IF NOT EXISTS company_concept_exposures_date_idx ON kg.company_concept_exposures (evidence_date DESC);

CREATE TABLE IF NOT EXISTS analytics.ai_hardware_discovery_candidates (
  candidate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_type text NOT NULL,
  document_id uuid REFERENCES corpus.documents(id) ON DELETE CASCADE,
  candidate_date date,
  name text NOT NULL,
  normalized_name text,
  parent_name text,
  concept_type text,
  relation_type text,
  stock_code text,
  stock_name text,
  company_relation_type text,
  evidence text,
  ai_context_terms text[] NOT NULL DEFAULT '{}'::text[],
  rule_score double precision,
  llm_score double precision,
  confidence double precision,
  status text NOT NULL DEFAULT 'new',
  discovered_by text NOT NULL DEFAULT 'rules:v2',
  llm_run_id uuid REFERENCES ai.extraction_runs(id) ON DELETE SET NULL,
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT discovery_candidates_type_chk CHECK (candidate_type IN ('concept', 'concept_edge', 'company_exposure')),
  CONSTRAINT discovery_candidates_status_chk CHECK (status IN ('new', 'classified', 'promoted', 'rejected', 'needs_review'))
);

COMMENT ON TABLE analytics.ai_hardware_discovery_candidates IS 'Candidate concepts, concept edges, and company-concept exposure facts discovered from documents before promotion into the knowledge graph.';
CREATE UNIQUE INDEX IF NOT EXISTS ai_hardware_discovery_candidates_hash_uniq
  ON analytics.ai_hardware_discovery_candidates (
    candidate_type,
    COALESCE(document_id, '00000000-0000-0000-0000-000000000000'::uuid),
    name,
    COALESCE(parent_name, ''),
    COALESCE(stock_code, ''),
    COALESCE(company_relation_type, ''),
    md5(COALESCE(evidence, ''))
  );
CREATE INDEX IF NOT EXISTS ai_hardware_discovery_candidates_status_idx ON analytics.ai_hardware_discovery_candidates (status, candidate_date DESC);
CREATE INDEX IF NOT EXISTS ai_hardware_discovery_candidates_name_idx ON analytics.ai_hardware_discovery_candidates (normalized_name);

CREATE TABLE IF NOT EXISTS analytics.ai_hardware_theme_scores (
  as_of_date date NOT NULL,
  concept_id uuid NOT NULL REFERENCES kg.concepts(concept_id) ON DELETE CASCADE,
  stock_code text,
  stock_name text,
  relation_type text,
  mention_count_30d integer NOT NULL DEFAULT 0,
  evidence_count_90d integer NOT NULL DEFAULT 0,
  research_report_count_180d integer NOT NULL DEFAULT 0,
  forecast_year integer,
  forecast_net_profit numeric,
  forecast_eps numeric,
  next_forecast_year integer,
  next_forecast_net_profit numeric,
  next_forecast_eps numeric,
  forecast_net_profit_growth double precision,
  forecast_eps_growth double precision,
  forward_pe numeric,
  latest_rating text,
  latest_rating_change text,
  technical_tags text[] NOT NULL DEFAULT '{}'::text[],
  score double precision NOT NULL DEFAULT 0,
  score_breakdown jsonb NOT NULL DEFAULT '{}'::jsonb,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (as_of_date, concept_id, stock_code)
);

COMMENT ON TABLE analytics.ai_hardware_theme_scores IS 'Final AI hardware company/theme scores from KG exposures, forecasts, research momentum, and technical features.';
CREATE INDEX IF NOT EXISTS ai_hardware_theme_scores_score_idx ON analytics.ai_hardware_theme_scores (as_of_date DESC, score DESC);
CREATE INDEX IF NOT EXISTS ai_hardware_theme_scores_stock_idx ON analytics.ai_hardware_theme_scores (stock_code, as_of_date DESC);
CREATE INDEX IF NOT EXISTS ai_hardware_theme_scores_relation_idx ON analytics.ai_hardware_theme_scores (relation_type, as_of_date DESC);

GRANT USAGE ON SCHEMA kg, analytics TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA kg, analytics TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA kg, analytics TO finance_app;

COMMIT;
