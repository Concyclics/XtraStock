BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='30s';
ALTER TABLE factor.daily_scores
 ADD COLUMN IF NOT EXISTS input_contract_version text NOT NULL DEFAULT 'legacy',
 ADD COLUMN IF NOT EXISTS quality_status text NOT NULL DEFAULT 'legacy',
 ADD COLUMN IF NOT EXISTS source_feature_updated_at timestamptz,
 ADD COLUMN IF NOT EXISTS source_factor_version integer,
 ADD COLUMN IF NOT EXISTS source_cohort_hash text,
 ADD COLUMN IF NOT EXISTS source_decision_cutoff timestamptz,
 ADD COLUMN IF NOT EXISTS invalid_reason text;
CREATE TABLE IF NOT EXISTS factor.daily_score_revisions (
 revision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 factor_id uuid NOT NULL,
 asset_id uuid NOT NULL,
 dt date NOT NULL,
 previous_row jsonb NOT NULL,
 replacement_reason text NOT NULL,
 archived_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS daily_score_revisions_lookup_idx
 ON factor.daily_score_revisions(factor_id,asset_id,dt,archived_at DESC);
COMMENT ON TABLE factor.daily_score_revisions IS
 'Append-only previous score rows captured atomically before replacement or NULL invalidation; no production eligibility is implied.';
COMMENT ON COLUMN factor.daily_scores.quality_status IS
 'legacy has no current source contract; valid requires strict current system features; invalid rows retain identity and provenance but raw/rank/zscore are NULL. Valid input does not mean approved factor or predictive edge.';
GRANT SELECT,INSERT ON factor.daily_score_revisions TO finance_app;
GRANT USAGE,SELECT ON SEQUENCE factor.daily_score_revisions_revision_id_seq TO finance_app;
GRANT SELECT ON factor.daily_score_revisions TO finance_ai_reader;
CREATE OR REPLACE VIEW factor.valid_daily_scores_v1 AS
WITH cohort_versions AS (
 SELECT f.dt,md5(string_agg(f.asset_id::text||':'||extract(epoch FROM f.updated_at)::text,
     ',' ORDER BY f.asset_id)) AS source_cohort_hash
 FROM factor.feature_daily f JOIN market.assets a USING(asset_id)
 WHERE a.asset_type='stock' AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
 GROUP BY f.dt
)
SELECT s.factor_id,s.asset_id,cv.dt,s.raw_value,s.percentile,s.zscore,
 s.available_at,s.source_feature_date,s.updated_at,s.input_contract_version,s.quality_status,
 s.source_feature_updated_at,s.source_factor_version,s.source_decision_cutoff,s.invalid_reason,
 s.source_cohort_hash
FROM factor.daily_scores s
JOIN factor.feature_daily f USING(asset_id,dt)
JOIN factor.definitions d USING(factor_id)
JOIN market.assets a USING(asset_id)
JOIN cohort_versions cv USING(dt)
WHERE s.quality_status='valid' AND s.input_contract_version='pit-input-v1'
 AND s.raw_value IS NOT NULL AND s.raw_value NOT IN ('Infinity'::float8,'-Infinity'::float8,'NaN'::float8)
 AND s.source_factor_version=d.version AND s.source_feature_updated_at=f.updated_at
 AND s.source_decision_cutoff=f.decision_cutoff AND s.source_cohort_hash=cv.source_cohort_hash
 AND s.available_at=f.available_at AND s.source_feature_date=f.dt
 AND f.input_contract_version='pit-input-v1' AND f.knowledge_mode='system'
 AND f.input_quality->>'strict_pit_ready'='true'
 AND f.available_at<=f.decision_cutoff AND f.decision_cutoff<=statement_timestamp()
 AND f.decision_cutoff<((f.dt+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
 AND a.asset_type='stock' AND NOT coalesce(a.metadata ? 'identity_alias_of',false);
COMMENT ON VIEW factor.valid_daily_scores_v1 IS
 'Scores with current strict system input and factor/cohort versions. Filter dt at consumers for bounded reads. Input validity is not production approval or profitability; percentile can be NULL for insufficient or constant cohorts.';
GRANT SELECT ON factor.valid_daily_scores_v1 TO finance_app,finance_ai_reader;
COMMIT;
