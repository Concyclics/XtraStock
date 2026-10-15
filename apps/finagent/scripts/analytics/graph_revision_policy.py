"""Review-state synchronization shared by the bounded repair and KG builder."""
from __future__ import annotations

GRAPH_WRITE_LOCK = 704201313
GRAPH_CONTRACT_VERSION = 'graph-pit-v1'


def acquire_graph_write_lock(cur, *, timeout_ms: int = 180000, lock_key: int = GRAPH_WRITE_LOCK):
    """Queue behind another writer, with a finite wait and transaction ownership.

    Restore the caller's row-lock timeout after acquisition. A timeout is a real
    failure and must roll back the transaction, never permit unlocked writes.
    """
    if timeout_ms <= 0:
        raise ValueError('graph lock wait must be positive')
    previous = cur.execute("SELECT current_setting('lock_timeout') AS value").fetchone()['value']
    cur.execute("SELECT set_config('lock_timeout',%s,true)", (str(timeout_ms),))
    cur.execute('SELECT pg_advisory_xact_lock(%s)', (lock_key,))
    cur.execute("SELECT set_config('lock_timeout',%s,true)", (previous,))

RECONCILE_EXPOSURES_SQL = """
WITH source_rows AS (
 SELECT v.relation_version_id,x.exposure_id,
   CASE WHEN x.status IN ('rejected','archived') THEN x.status
        WHEN x.status='active' AND x.metadata->'attribution_check'->>'supported'='true' THEN 'active'
        ELSE 'candidate' END AS new_status,
   CASE WHEN x.status IN ('rejected','archived') THEN 'rejected'
        WHEN x.review_status IN ('approved','rejected','disputed') THEN x.review_status
        WHEN v.review_status='classified' AND x.status='active'
          AND x.metadata->'attribution_check'->>'supported'='true'
          AND v.source_system='kg.company_concept_exposures' THEN 'classified'
        ELSE 'unreviewed' END AS new_review,
   v.attributes||jsonb_build_object('exposure_id',x.exposure_id,
     'attribution_check',coalesce(x.metadata->'attribution_check',jsonb_build_object('supported',false,'reason','missing_source_attribution')),
     'source_review_status',x.status,'review_contract','graph-pit-v1',
     'superseded_by_canonical',v.source_system='company_concept_exposures') AS new_attributes
 FROM kg.relation_versions v JOIN kg.relations r USING(relation_id)
 JOIN kg.company_concept_exposures x ON x.exposure_id=CASE
   WHEN v.source_system='company_concept_exposures' AND r.relation_key ~ '^relation:exposure:[0-9a-f-]{36}$'
     THEN split_part(r.relation_key,':',3)::uuid
   WHEN v.source_system='kg.company_concept_exposures' AND v.attributes->>'exposure_id' ~ '^[0-9a-f-]{36}$'
     THEN (v.attributes->>'exposure_id')::uuid END
 WHERE v.source_system IN ('company_concept_exposures','kg.company_concept_exposures')
)
UPDATE kg.relation_versions v SET status=s.new_status,review_status=s.new_review,
 attributes=s.new_attributes,updated_at=now()
FROM source_rows s WHERE v.relation_version_id=s.relation_version_id
 AND (v.status,v.review_status,v.attributes) IS DISTINCT FROM (s.new_status,s.new_review,s.new_attributes)
"""
