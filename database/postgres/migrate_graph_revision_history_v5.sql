BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE kg.relation_versions ADD COLUMN IF NOT EXISTS evidence_updated_at timestamptz;

CREATE TABLE IF NOT EXISTS kg.version_history_contract (
  contract_key text PRIMARY KEY,
  installed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  semantics text NOT NULL
);
INSERT INTO kg.version_history_contract(contract_key,semantics)
VALUES('revision-v5','Strict system replay is supported only from this installation onward; older public reconstruction is not live history.')
ON CONFLICT(contract_key) DO NOTHING;
GRANT SELECT ON kg.version_history_contract TO finance_app,finance_ai_reader;

CREATE TABLE IF NOT EXISTS kg.version_revision_history (
  revision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  version_kind text NOT NULL CHECK (version_kind IN ('relation','entity')),
  source_version_id uuid NOT NULL,
  system_known_during tstzrange NOT NULL CHECK (NOT isempty(system_known_during)),
  frozen_record jsonb NOT NULL,
  frozen_evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  closed_at timestamptz NOT NULL,
  UNIQUE(version_kind,source_version_id,closed_at)
);
CREATE INDEX IF NOT EXISTS version_revision_history_source_idx
  ON kg.version_revision_history(version_kind,source_version_id,closed_at DESC);
CREATE INDEX IF NOT EXISTS version_revision_history_system_idx
  ON kg.version_revision_history USING gist(system_known_during);
COMMENT ON TABLE kg.version_revision_history IS
 'Immutable prior revisions of the compatibility current-head tables. System replay must union these closed revisions with current heads; no history is invented before installation.';

CREATE OR REPLACE FUNCTION kg.reject_revision_history_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'version revision history is append-only; restore by writing a new current-head revision';
END;
$$;
DROP TRIGGER IF EXISTS version_revision_history_immutable ON kg.version_revision_history;
CREATE TRIGGER version_revision_history_immutable BEFORE UPDATE OR DELETE
  ON kg.version_revision_history FOR EACH ROW EXECUTE FUNCTION kg.reject_revision_history_mutation();

CREATE OR REPLACE FUNCTION kg.preserve_version_revision()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,kg AS $$
DECLARE
  kind text;
  identity_value uuid;
  closed_time timestamptz;
  closed_range tstzrange;
  evidence_snapshot jsonb := '[]'::jsonb;
BEGIN
  IF TG_OP='INSERT' THEN
    -- Processing readiness is when this version is actually materialized.
    -- Public-time reconstruction remains available through public_known_during.
    NEW.system_known_during := tstzrange(
      greatest(coalesce(lower(NEW.system_known_during),statement_timestamp()),statement_timestamp()),NULL,'[)');
    RETURN NEW;
  END IF;
  IF (to_jsonb(NEW)-ARRAY['updated_at','system_known_during'])
       IS NOT DISTINCT FROM (to_jsonb(OLD)-ARRAY['updated_at','system_known_during']) THEN
    -- A replay of identical input must not create a revision or rewrite the head/WAL.
    RETURN NULL;
  END IF;
  IF NOT upper_inf(OLD.system_known_during) THEN
    RAISE EXCEPTION 'current-head system interval must remain open';
  END IF;
  closed_time := greatest(clock_timestamp(),lower(OLD.system_known_during)+interval '1 microsecond');
  closed_range := tstzrange(lower(OLD.system_known_during),closed_time,'[)');
  IF TG_TABLE_NAME='relation_versions' THEN
    kind := 'relation'; identity_value := OLD.relation_version_id;
    SELECT coalesce(jsonb_agg(to_jsonb(e) ORDER BY e.evidence_id),'[]'::jsonb)
      INTO evidence_snapshot FROM kg.relation_version_evidence e
      WHERE e.relation_version_id=identity_value;
  ELSE
    kind := 'entity'; identity_value := OLD.entity_version_id;
  END IF;
  INSERT INTO kg.version_revision_history
    (version_kind,source_version_id,system_known_during,frozen_record,frozen_evidence,closed_at)
  VALUES(kind,identity_value,closed_range,
         to_jsonb(OLD)||jsonb_build_object('system_known_during',closed_range::text),
         evidence_snapshot,closed_time);
  NEW.system_known_during := tstzrange(closed_time,NULL,'[)');
  RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS preserve_relation_version_revision ON kg.relation_versions;
CREATE TRIGGER preserve_relation_version_revision BEFORE INSERT OR UPDATE
  ON kg.relation_versions FOR EACH ROW EXECUTE FUNCTION kg.preserve_version_revision();
DROP TRIGGER IF EXISTS preserve_entity_version_revision ON kg.entity_versions;
CREATE TRIGGER preserve_entity_version_revision BEFORE INSERT OR UPDATE
  ON kg.entity_versions FOR EACH ROW EXECUTE FUNCTION kg.preserve_version_revision();

-- Evidence-only corrections must also close a head revision before altering its provenance.
CREATE OR REPLACE FUNCTION kg.preserve_evidence_revision()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,kg AS $$
BEGIN
 IF TG_OP='INSERT' THEN
   NEW.system_at := greatest(NEW.system_at,clock_timestamp());
   RETURN NEW;
 END IF;
 IF TG_OP='UPDATE' THEN
   IF NEW.relation_version_id IS DISTINCT FROM OLD.relation_version_id THEN
     RAISE EXCEPTION 'evidence cannot be moved between relation versions';
   END IF;
   IF (to_jsonb(NEW)-ARRAY['system_at','created_at']) IS NOT DISTINCT FROM (to_jsonb(OLD)-ARRAY['system_at','created_at']) THEN
     RETURN NULL;
   END IF;
 END IF;
 UPDATE kg.relation_versions SET evidence_updated_at=clock_timestamp()
   WHERE relation_version_id=OLD.relation_version_id;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 NEW.system_at := greatest(NEW.system_at,clock_timestamp());
 RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS preserve_relation_evidence_revision ON kg.relation_version_evidence;
CREATE TRIGGER preserve_relation_evidence_revision BEFORE INSERT OR UPDATE OR DELETE
 ON kg.relation_version_evidence FOR EACH ROW EXECUTE FUNCTION kg.preserve_evidence_revision();

CREATE OR REPLACE FUNCTION kg.relation_versions_known_at(p_known_at timestamptz,p_knowledge_mode text DEFAULT 'public')
RETURNS SETOF kg.relation_versions LANGUAGE sql STABLE AS $$
 SELECT v.* FROM kg.relation_versions v
 WHERE (p_knowledge_mode='system' AND v.system_known_during @> p_known_at)
    OR (p_knowledge_mode='public' AND v.public_known_during @> p_known_at)
 UNION ALL
 SELECT r.* FROM kg.version_revision_history h
 CROSS JOIN LATERAL jsonb_populate_record(NULL::kg.relation_versions,h.frozen_record) r
 WHERE p_knowledge_mode='system' AND h.version_kind='relation' AND h.system_known_during @> p_known_at;
$$;

-- Preserve signatures used by Agent tools, but formal retrieval now excludes candidates.
CREATE OR REPLACE FUNCTION kg.temporal_edges_at(
  p_valid_at date,p_known_at timestamptz,p_knowledge_mode text DEFAULT 'public',
  p_entity_ids uuid[] DEFAULT NULL,p_relation_types text[] DEFAULT NULL,
  p_min_confidence double precision DEFAULT 0,p_limit integer DEFAULT 200)
RETURNS TABLE (
  relation_id uuid,relation_version_id uuid,relation_key text,
  source_entity_id uuid,source_name text,source_type text,target_entity_id uuid,target_name text,target_type text,
  relation_type text,valid_during daterange,public_known_during tstzrange,system_known_during tstzrange,
  direction smallint,confidence double precision,weight double precision,status text,
  source_system text,extractor_version text,attributes jsonb)
LANGUAGE sql STABLE AS $$
 WITH selected AS (
   SELECT DISTINCT ON(r.source_entity_id,r.target_entity_id,r.relation_type,
           CASE WHEN r.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO','PRICE_LINKED_TO') THEN '' ELSE v.version_key END)
     r.relation_id,v.relation_version_id,r.relation_key,r.source_entity_id,r.target_entity_id,r.relation_type,
     v.valid_during,v.public_known_during,v.system_known_during,v.direction,v.confidence,v.weight,v.status,
     v.source_system,v.extractor_version,v.attributes
   FROM kg.relations r JOIN kg.relation_versions_known_at(p_known_at,p_knowledge_mode) v USING(relation_id)
   WHERE v.valid_during @> p_valid_at AND v.status='active'
     AND coalesce(v.attributes->'attribution_check'->>'supported','true')<>'false'
     AND coalesce(v.attributes->>'superseded_by_canonical','false')<>'true'
     AND (p_entity_ids IS NULL OR r.source_entity_id=ANY(p_entity_ids) OR r.target_entity_id=ANY(p_entity_ids))
     AND (p_relation_types IS NULL OR r.relation_type=ANY(p_relation_types)) AND v.confidence>=p_min_confidence
   ORDER BY r.source_entity_id,r.target_entity_id,r.relation_type,
     CASE WHEN r.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO','PRICE_LINKED_TO') THEN '' ELSE v.version_key END,
     (v.source_system='kg.company_concept_exposures') DESC,v.confidence DESC,lower(v.system_known_during) DESC
 )
 SELECT r.relation_id,r.relation_version_id,r.relation_key,r.source_entity_id,se.name,se.entity_type,
   r.target_entity_id,te.name,te.entity_type,r.relation_type,r.valid_during,r.public_known_during,
   r.system_known_during,r.direction,r.confidence,r.weight,r.status,r.source_system,r.extractor_version,r.attributes
 FROM selected r JOIN kg.market_entities se ON se.entity_id=r.source_entity_id
 JOIN kg.market_entities te ON te.entity_id=r.target_entity_id
 ORDER BY r.confidence DESC,r.relation_type,r.relation_key LIMIT greatest(1,least(coalesce(p_limit,200),2000));
$$;
CREATE OR REPLACE FUNCTION kg.temporal_edges_between(
 p_start_date date,p_end_date date,p_known_at timestamptz,p_knowledge_mode text DEFAULT 'public',
 p_entity_ids uuid[] DEFAULT NULL,p_relation_types text[] DEFAULT NULL,
 p_min_confidence double precision DEFAULT 0,p_limit integer DEFAULT 500)
RETURNS SETOF kg.relation_versions LANGUAGE sql STABLE AS $$
 SELECT v.* FROM kg.relation_versions_known_at(p_known_at,p_knowledge_mode) v JOIN kg.relations r USING(relation_id)
 WHERE v.valid_during && daterange(p_start_date,p_end_date+1,'[)') AND v.status='active'
   AND coalesce(v.attributes->'attribution_check'->>'supported','true')<>'false'
   AND coalesce(v.attributes->>'superseded_by_canonical','false')<>'true'
   AND (p_entity_ids IS NULL OR r.source_entity_id=ANY(p_entity_ids) OR r.target_entity_id=ANY(p_entity_ids))
   AND (p_relation_types IS NULL OR r.relation_type=ANY(p_relation_types)) AND v.confidence>=p_min_confidence
 ORDER BY lower(v.valid_during),v.confidence DESC LIMIT greatest(1,least(coalesce(p_limit,500),5000));
$$;
REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON kg.version_revision_history FROM finance_app,finance_ai_reader;
GRANT SELECT ON kg.version_revision_history TO finance_app,finance_ai_reader;
GRANT EXECUTE ON FUNCTION kg.relation_versions_known_at(timestamptz,text) TO finance_app,finance_ai_reader;
COMMENT ON TABLE kg.relation_versions IS
 'Current compatibility heads. Updates freeze the previous record and evidence in kg.version_revision_history; replay system-time through kg.relation_versions_known_at.';
COMMIT;
