BEGIN;

ALTER TABLE kg.company_concept_exposures
  ADD COLUMN IF NOT EXISTS business_role text,
  ADD COLUMN IF NOT EXISTS directness text NOT NULL DEFAULT 'unknown',
  ADD COLUMN IF NOT EXISTS evidence_span text,
  ADD COLUMN IF NOT EXISTS extractor_version text,
  ADD COLUMN IF NOT EXISTS review_status text NOT NULL DEFAULT 'unreviewed';

UPDATE kg.company_concept_exposures
SET business_role = COALESCE(business_role, relation_type),
    directness = CASE
      WHEN relation_type = 'weak_mention' THEN 'indirect'
      WHEN status = 'active' THEN 'direct'
      ELSE directness
    END,
    evidence_span = COALESCE(evidence_span, evidence),
    extractor_version = COALESCE(extractor_version, source),
    review_status = CASE
      WHEN status = 'rejected' THEN 'rejected'
      WHEN status = 'active' AND review_status = 'unreviewed' THEN 'legacy_active'
      ELSE review_status
    END;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'company_concept_directness_chk'
  ) THEN
    ALTER TABLE kg.company_concept_exposures
      ADD CONSTRAINT company_concept_directness_chk
      CHECK (directness IN ('direct', 'indirect', 'unknown'));
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS company_concept_exposures_review_idx
  ON kg.company_concept_exposures (status, review_status, directness);

COMMIT;
