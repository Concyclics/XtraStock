BEGIN;

CREATE SCHEMA IF NOT EXISTS kg;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS kg.relation_types (
  relation_type text PRIMARY KEY,
  source_entity_types text[] NOT NULL DEFAULT '{}'::text[],
  target_entity_types text[] NOT NULL DEFAULT '{}'::text[],
  description text NOT NULL,
  default_direction smallint NOT NULL DEFAULT 0,
  default_lag_days integer NOT NULL DEFAULT 0,
  default_horizon_days integer,
  decay_half_life_days double precision,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT relation_types_direction_chk CHECK (default_direction IN (-1, 0, 1))
);

INSERT INTO kg.relation_types
  (relation_type, source_entity_types, target_entity_types, description, default_direction, default_lag_days, default_horizon_days, decay_half_life_days)
VALUES
  ('IN_INDUSTRY', ARRAY['company'], ARRAY['industry'], '公司属于行业或细分行业。', 0, 0, NULL, NULL),
  ('EXPOSED_TO', ARRAY['company'], ARRAY['concept','product','material','equipment','technology'], '公司对主题、产品、材料、设备或技术存在业务暴露。', 0, 0, 365, 180),
  ('CONTAINS', ARRAY['concept','product','material','equipment','technology'], ARRAY['concept','product','material','equipment','technology'], '主题、板块或产业节点包含更细粒度节点。', 0, 0, NULL, NULL),
  ('UPSTREAM_OF', ARRAY['company','industry','concept','product','material','equipment','technology'], ARRAY['company','industry','concept','product','material','equipment','technology'], '供应链或技术链上游关系。', 1, 30, 365, 180),
  ('DOWNSTREAM_OF', ARRAY['company','industry','concept','product','material','equipment','technology'], ARRAY['company','industry','concept','product','material','equipment','technology'], '供应链或技术链下游关系。', 1, 30, 365, 180),
  ('USED_IN', ARRAY['product','material','equipment','technology','concept'], ARRAY['product','equipment','technology','concept','industry'], '材料、部件或技术被用于目标产品、设备、主题或行业。', 0, 0, 365, 180),
  ('COMPONENT_OF', ARRAY['product','material','equipment','technology','concept'], ARRAY['product','equipment','technology','concept'], '部件、材料或技术构成目标节点。', 0, 0, 365, 180),
  ('PRODUCES', ARRAY['company'], ARRAY['product','material','equipment'], '公司生产目标产品、材料或设备。', 0, 0, 365, 180),
  ('USES', ARRAY['company','product','process'], ARRAY['material','equipment','technology'], '生产过程或公司使用目标材料、设备或技术。', 0, 0, 365, 180),
  ('SUPPLIES_TO', ARRAY['company'], ARRAY['company','industry'], '供应商向客户或行业供货。', 1, 30, 365, 180),
  ('CUSTOMER_OF', ARRAY['company'], ARRAY['company'], '客户关系的反向表达。', 0, 0, 365, 180),
  ('SUBSTITUTE_FOR', ARRAY['product','material','technology'], ARRAY['product','material','technology'], '产品、材料或技术具有替代关系。', 0, 90, 730, 365),
  ('BENEFITS_FROM', ARRAY['company','industry','concept'], ARRAY['event','policy','macro_indicator','technology'], '主体可能受益于事件、政策或技术变化。', 1, 0, 180, 60),
  ('PRICE_LINKED_TO', ARRAY['company','industry','product'], ARRAY['commodity','material'], '盈利或估值与商品或材料价格相关。', 0, 0, 365, 180),
  ('FORECASTS', ARRAY['institution','analyst'], ARRAY['company','industry','macro_indicator'], '机构或分析师给出预测、评级或目标价。', 0, 0, 365, 90),
  ('RATES', ARRAY['institution','analyst'], ARRAY['company','industry'], '机构或分析师对公司或行业给出评级、评级变动与目标价。', 0, 0, 180, 60),
  ('CLASSIFIED_AS', ARRAY['company'], ARRAY['industry'], '研报在特定时点对公司的行业分类观察。', 0, 0, 365, 180),
  ('AFFECTED_BY_EVENT', ARRAY['company','industry'], ARRAY['event'], '公司或行业受到已识别事件影响。', 0, 0, 30, 15),
  ('COVERS_COMPANY', ARRAY['institution','analyst'], ARRAY['company'], '机构或分析师发布覆盖公司的研报。', 0, 0, 180, 60)
ON CONFLICT (relation_type) DO UPDATE SET
  source_entity_types=EXCLUDED.source_entity_types,
  target_entity_types=EXCLUDED.target_entity_types,
  description=EXCLUDED.description,
  default_direction=EXCLUDED.default_direction,
  default_lag_days=EXCLUDED.default_lag_days,
  default_horizon_days=EXCLUDED.default_horizon_days,
  decay_half_life_days=EXCLUDED.decay_half_life_days,
  updated_at=now();

CREATE TABLE IF NOT EXISTS kg.relation_claims (
  claim_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  claim_key text NOT NULL UNIQUE,
  source_entity_id uuid NOT NULL REFERENCES kg.market_entities(entity_id) ON DELETE CASCADE,
  target_entity_id uuid NOT NULL REFERENCES kg.market_entities(entity_id) ON DELETE CASCADE,
  relation_type text NOT NULL REFERENCES kg.relation_types(relation_type),
  valid_from date NOT NULL,
  valid_to date,
  known_at timestamptz NOT NULL,
  expired_at timestamptz,
  direction smallint NOT NULL DEFAULT 0,
  confidence double precision NOT NULL,
  weight double precision,
  lag_days integer NOT NULL DEFAULT 0,
  horizon_days integer,
  numeric_value numeric,
  unit text,
  currency text,
  directness text,
  source_system text NOT NULL,
  extractor_version text NOT NULL,
  review_status text NOT NULL DEFAULT 'unreviewed',
  status text NOT NULL DEFAULT 'candidate',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT relation_claim_dates_chk CHECK (valid_to IS NULL OR valid_to >= valid_from),
  CONSTRAINT relation_claim_direction_chk CHECK (direction IN (-1, 0, 1)),
  CONSTRAINT relation_claim_confidence_chk CHECK (confidence BETWEEN 0 AND 1),
  CONSTRAINT relation_claim_review_chk CHECK (review_status IN ('unreviewed','approved','rejected','disputed')),
  CONSTRAINT relation_claim_status_chk CHECK (status IN ('candidate','active','expired','rejected','archived'))
);

COMMENT ON TABLE kg.relation_claims IS
  'Bitemporal relation assertions. valid_* describes the world; known_at/expired_at describes when the database knew the claim.';
CREATE INDEX IF NOT EXISTS relation_claims_source_time_idx
  ON kg.relation_claims (source_entity_id, relation_type, known_at DESC);
CREATE INDEX IF NOT EXISTS relation_claims_target_time_idx
  ON kg.relation_claims (target_entity_id, relation_type, known_at DESC);
CREATE INDEX IF NOT EXISTS relation_claims_pit_idx
  ON kg.relation_claims (known_at, valid_from, valid_to)
  WHERE status IN ('candidate','active');
CREATE INDEX IF NOT EXISTS relation_claims_metadata_idx ON kg.relation_claims USING gin (metadata);

CREATE TABLE IF NOT EXISTS kg.claim_evidence (
  evidence_id bigserial PRIMARY KEY,
  claim_id uuid NOT NULL REFERENCES kg.relation_claims(claim_id) ON DELETE CASCADE,
  document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  chunk_id uuid,
  source_published_at timestamptz,
  evidence_text text,
  span_start integer,
  span_end integer,
  confidence double precision,
  evidence_hash text NOT NULL,
  extraction_run_id uuid REFERENCES ai.extraction_runs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT claim_evidence_confidence_chk CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  UNIQUE (claim_id, evidence_hash)
);

COMMENT ON TABLE kg.claim_evidence IS
  'Auditable evidence for relation claims. A canonical edge may have multiple independent documents or spans.';
CREATE INDEX IF NOT EXISTS claim_evidence_document_idx ON kg.claim_evidence (document_id, claim_id);
CREATE INDEX IF NOT EXISTS claim_evidence_claim_idx ON kg.claim_evidence (claim_id, source_published_at DESC);

CREATE TABLE IF NOT EXISTS analytics.graph_feature_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  dt date NOT NULL,
  direct_exposure_score double precision NOT NULL DEFAULT 0,
  exposure_count integer NOT NULL DEFAULT 0,
  positive_event_score_30d double precision NOT NULL DEFAULT 0,
  negative_event_score_30d double precision NOT NULL DEFAULT 0,
  institution_coverage_30d integer NOT NULL DEFAULT 0,
  evidence_document_count_30d integer NOT NULL DEFAULT 0,
  new_relation_count_30d integer NOT NULL DEFAULT 0,
  novelty_score_30d double precision NOT NULL DEFAULT 0,
  graph_score double precision NOT NULL DEFAULT 0,
  available_at timestamptz NOT NULL,
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, dt)
);

COMMENT ON TABLE analytics.graph_feature_daily IS
  'Point-in-time company graph features. Only claims known by the daily cutoff contribute.';
CREATE INDEX IF NOT EXISTS graph_feature_daily_dt_score_idx
  ON analytics.graph_feature_daily (dt DESC, graph_score DESC);

GRANT USAGE ON SCHEMA kg, analytics TO finance_app, finance_ai_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON kg.relation_types, kg.relation_claims, kg.claim_evidence, analytics.graph_feature_daily TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA kg TO finance_app;
GRANT SELECT ON kg.relation_types, kg.relation_claims, kg.claim_evidence, analytics.graph_feature_daily TO finance_ai_reader;

COMMIT;
