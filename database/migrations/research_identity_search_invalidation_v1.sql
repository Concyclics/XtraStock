BEGIN;
-- Canonical identity can change without a document-body update. Keep that
-- derived-index invalidation in the same transaction as the mapping change.
CREATE OR REPLACE FUNCTION ops.enqueue_research_identity_search() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='UPDATE' AND ROW(NEW.new_code,NEW.asset_id,NEW.security_name)
      IS NOT DISTINCT FROM ROW(OLD.new_code,OLD.asset_id,OLD.security_name) THEN
    RETURN NEW;
  END IF;
  INSERT INTO ops.document_index_queue(document_id,operation,status,priority,dirty_at,available_at,metadata)
  SELECT document_id,'upsert','pending',50,now(),now(),
         jsonb_build_object('reason','canonical_security_identity_changed')
  FROM research.reports WHERE stock_code=NEW.old_code
  ON CONFLICT(document_id) DO UPDATE SET operation='upsert',
    status=CASE WHEN ops.document_index_queue.status='running' THEN 'running' ELSE 'pending' END,
    priority=least(ops.document_index_queue.priority,excluded.priority),
    version=ops.document_index_queue.version+1,dirty_at=now(),available_at=now(),
    retry_count=0,last_error=NULL,
    metadata=ops.document_index_queue.metadata||excluded.metadata;
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS research_identity_search_changed ON market.research_security_code_aliases;
CREATE TRIGGER research_identity_search_changed
AFTER INSERT OR UPDATE OF new_code,asset_id,security_name ON market.research_security_code_aliases
FOR EACH ROW EXECUTE FUNCTION ops.enqueue_research_identity_search();
COMMIT;
