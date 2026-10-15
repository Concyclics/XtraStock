-- Run inside the caller's transaction under the existing graph writer lock.
ALTER TABLE kg.relation_versions DROP CONSTRAINT IF EXISTS relation_versions_review_chk;
ALTER TABLE kg.relation_versions ADD CONSTRAINT relation_versions_review_chk
 CHECK (review_status IN ('unreviewed','classified','approved','rejected','disputed'));
COMMENT ON COLUMN kg.relation_versions.review_status IS
 'classified = automatically checked source provenance, not approved business facts. approved remains explicit review. Immutable revisions preserve when each decision became known.';
