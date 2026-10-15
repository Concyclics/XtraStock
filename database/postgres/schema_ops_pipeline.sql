BEGIN;

CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.crawler_runs (
  id bigserial PRIMARY KEY,
  task_name text NOT NULL,
  source_name text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  status text NOT NULL DEFAULT 'running',
  start_date date,
  end_date date,
  new_rows integer,
  log_path text,
  error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT crawler_runs_status_chk CHECK (status IN ('running', 'ok', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS crawler_runs_task_started_idx
  ON ops.crawler_runs(task_name, started_at DESC);

CREATE INDEX IF NOT EXISTS crawler_runs_source_status_idx
  ON ops.crawler_runs(source_name, status, started_at DESC);

CREATE TABLE IF NOT EXISTS ops.pipeline_watermarks (
  pipeline_name text PRIMARY KEY,
  status text NOT NULL DEFAULT 'pending',
  last_success_at timestamptz,
  source_high_watermark timestamptz,
  dirty_from_date date,
  dirty_to_date date,
  last_run_id bigint REFERENCES ops.crawler_runs(id) ON DELETE SET NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT pipeline_watermarks_status_chk
    CHECK (status IN ('pending', 'running', 'ok', 'failed'))
);

CREATE INDEX IF NOT EXISTS pipeline_watermarks_status_idx
  ON ops.pipeline_watermarks(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS ops.ocr_queue (
  id bigserial PRIMARY KEY,
  source_name text NOT NULL DEFAULT 'eastmoney',
  report_type text NOT NULL,
  pdf_path text NOT NULL UNIQUE,
  md_path text NOT NULL,
  quality_json_path text NOT NULL,
  quality_md_path text NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  priority integer NOT NULL DEFAULT 100,
  retry_count integer NOT NULL DEFAULT 0,
  last_error text,
  discovered_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT ocr_queue_status_chk CHECK (status IN ('pending', 'running', 'ok', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS ocr_queue_status_priority_idx
  ON ops.ocr_queue(status, priority, discovered_at);

CREATE INDEX IF NOT EXISTS ocr_queue_report_type_idx
  ON ops.ocr_queue(report_type, status);

-- Documents become searchable through two independent stores: PostgreSQL FTS
-- and Meilisearch. Keep a durable, versioned dirty queue so crawlers and OCR
-- only re-index changed documents instead of replaying a rolling date window.
CREATE TABLE IF NOT EXISTS ops.document_index_queue (
  document_id uuid PRIMARY KEY,
  operation text NOT NULL DEFAULT 'upsert',
  status text NOT NULL DEFAULT 'pending',
  priority integer NOT NULL DEFAULT 100,
  retry_count integer NOT NULL DEFAULT 0,
  version bigint NOT NULL DEFAULT 1,
  claimed_version bigint,
  dirty_at timestamptz NOT NULL DEFAULT now(),
  available_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz,
  indexed_at timestamptz,
  last_error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT document_index_queue_operation_chk CHECK (operation IN ('upsert', 'delete')),
  CONSTRAINT document_index_queue_status_chk CHECK (status IN ('pending', 'running', 'ok', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS document_index_queue_pending_idx
  ON ops.document_index_queue (priority, available_at, dirty_at, document_id)
  WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS document_index_queue_status_idx
  ON ops.document_index_queue (status, dirty_at DESC);

CREATE OR REPLACE FUNCTION ops.enqueue_document_index(
  p_document_id uuid,
  p_reason text,
  p_priority integer DEFAULT 100
) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  IF p_document_id IS NULL THEN
    RETURN;
  END IF;

  INSERT INTO ops.document_index_queue (
    document_id, operation, status, priority, dirty_at, available_at, metadata
  )
  VALUES (
    p_document_id, 'upsert', 'pending', p_priority, now(), now(),
    jsonb_build_object('reason', p_reason)
  )
  ON CONFLICT (document_id) DO UPDATE
  SET operation = 'upsert',
      status = CASE
        WHEN ops.document_index_queue.status = 'running' THEN 'running'
        ELSE 'pending'
      END,
      priority = LEAST(ops.document_index_queue.priority, EXCLUDED.priority),
      retry_count = CASE
        WHEN ops.document_index_queue.status = 'failed' THEN 0
        ELSE ops.document_index_queue.retry_count
      END,
      version = ops.document_index_queue.version + 1,
      dirty_at = now(),
      available_at = now(),
      last_error = NULL,
      metadata = ops.document_index_queue.metadata || EXCLUDED.metadata;
END;
$$;

CREATE OR REPLACE FUNCTION ops.enqueue_document_delete(
  p_document_id uuid,
  p_reason text,
  p_priority integer DEFAULT 100
) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
  IF p_document_id IS NULL THEN
    RETURN;
  END IF;

  INSERT INTO ops.document_index_queue (
    document_id, operation, status, priority, dirty_at, available_at, metadata
  )
  VALUES (
    p_document_id, 'delete', 'pending', p_priority, now(), now(),
    jsonb_build_object('reason', p_reason)
  )
  ON CONFLICT (document_id) DO UPDATE
  SET operation = 'delete',
      status = CASE
        WHEN ops.document_index_queue.status = 'running' THEN 'running'
        ELSE 'pending'
      END,
      priority = LEAST(ops.document_index_queue.priority, EXCLUDED.priority),
      version = ops.document_index_queue.version + 1,
      dirty_at = now(),
      available_at = now(),
      last_error = NULL,
      metadata = ops.document_index_queue.metadata || EXCLUDED.metadata;
END;
$$;

CREATE OR REPLACE FUNCTION ops.document_index_documents_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    PERFORM ops.enqueue_document_delete(OLD.id, 'corpus.documents.delete');
    RETURN OLD;
  END IF;

  IF TG_OP = 'UPDATE'
     AND ROW(
       NEW.source_type, NEW.source_name, NEW.external_id, NEW.title, NEW.url,
       NEW.published_at, NEW.content_text, NEW.content_md, NEW.raw_text,
       NEW.metadata, NEW.content_hash, NEW.ingest_status
     ) IS NOT DISTINCT FROM ROW(
       OLD.source_type, OLD.source_name, OLD.external_id, OLD.title, OLD.url,
       OLD.published_at, OLD.content_text, OLD.content_md, OLD.raw_text,
       OLD.metadata, OLD.content_hash, OLD.ingest_status
     ) THEN
    RETURN NEW;
  END IF;

  PERFORM ops.enqueue_document_index(NEW.id, 'corpus.documents.' || lower(TG_OP));
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ops.document_index_reports_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    PERFORM ops.enqueue_document_index(OLD.document_id, 'research.reports.delete');
    RETURN OLD;
  END IF;

  IF TG_OP = 'UPDATE'
     AND ROW(
       NEW.report_type, NEW.report_date, NEW.report_title, NEW.institution,
       NEW.analyst_names, NEW.rating, NEW.rating_change, NEW.industry_name,
       NEW.stock_code, NEW.stock_name, NEW.source_url, NEW.web_text_status,
       NEW.pdf_status, NEW.ocr_status, NEW.metadata
     ) IS NOT DISTINCT FROM ROW(
       OLD.report_type, OLD.report_date, OLD.report_title, OLD.institution,
       OLD.analyst_names, OLD.rating, OLD.rating_change, OLD.industry_name,
       OLD.stock_code, OLD.stock_name, OLD.source_url, OLD.web_text_status,
       OLD.pdf_status, OLD.ocr_status, OLD.metadata
     ) THEN
    RETURN NEW;
  END IF;

  PERFORM ops.enqueue_document_index(NEW.document_id, 'research.reports.' || lower(TG_OP));
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ops.document_index_keywords_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    PERFORM ops.enqueue_document_index(OLD.document_id, 'corpus.document_keywords.delete');
    RETURN OLD;
  END IF;

  PERFORM ops.enqueue_document_index(NEW.document_id, 'corpus.document_keywords.' || lower(TG_OP));
  RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION ops.document_index_events_trigger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    PERFORM ops.enqueue_document_index(OLD.document_id, 'analytics.document_events.delete');
    RETURN OLD;
  END IF;

  IF TG_OP = 'UPDATE'
     AND ROW(
       NEW.document_id, NEW.event_date, NEW.event_type, NEW.polarity,
       NEW.impact_score, NEW.confidence, NEW.entity_texts, NEW.asset_ids,
       NEW.stock_codes, NEW.industries, NEW.keywords, NEW.summary,
       NEW.evidence, NEW.extraction_method, NEW.event_hash, NEW.metadata
     ) IS NOT DISTINCT FROM ROW(
       OLD.document_id, OLD.event_date, OLD.event_type, OLD.polarity,
       OLD.impact_score, OLD.confidence, OLD.entity_texts, OLD.asset_ids,
       OLD.stock_codes, OLD.industries, OLD.keywords, OLD.summary,
       OLD.evidence, OLD.extraction_method, OLD.event_hash, OLD.metadata
     ) THEN
    RETURN NEW;
  END IF;

  PERFORM ops.enqueue_document_index(NEW.document_id, 'analytics.document_events.' || lower(TG_OP));
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF to_regclass('corpus.documents') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM pg_trigger
       WHERE tgrelid = 'corpus.documents'::regclass
         AND tgname = 'document_index_documents_enqueue'
         AND NOT tgisinternal
     ) THEN
    CREATE TRIGGER document_index_documents_enqueue
      AFTER INSERT OR UPDATE OR DELETE ON corpus.documents
      FOR EACH ROW EXECUTE FUNCTION ops.document_index_documents_trigger();
  END IF;

  IF to_regclass('research.reports') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM pg_trigger
       WHERE tgrelid = 'research.reports'::regclass
         AND tgname = 'document_index_reports_enqueue'
         AND NOT tgisinternal
     ) THEN
    CREATE TRIGGER document_index_reports_enqueue
      AFTER INSERT OR UPDATE OR DELETE ON research.reports
      FOR EACH ROW EXECUTE FUNCTION ops.document_index_reports_trigger();
  END IF;

  IF to_regclass('corpus.document_keywords') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM pg_trigger
       WHERE tgrelid = 'corpus.document_keywords'::regclass
         AND tgname = 'document_index_keywords_enqueue'
         AND NOT tgisinternal
     ) THEN
    CREATE TRIGGER document_index_keywords_enqueue
      AFTER INSERT OR UPDATE OR DELETE ON corpus.document_keywords
      FOR EACH ROW EXECUTE FUNCTION ops.document_index_keywords_trigger();
  END IF;

  IF to_regclass('analytics.document_events') IS NOT NULL
     AND NOT EXISTS (
       SELECT 1 FROM pg_trigger
       WHERE tgrelid = 'analytics.document_events'::regclass
         AND tgname = 'document_index_events_enqueue'
         AND NOT tgisinternal
     ) THEN
    CREATE TRIGGER document_index_events_enqueue
      AFTER INSERT OR UPDATE OR DELETE ON analytics.document_events
      FOR EACH ROW EXECUTE FUNCTION ops.document_index_events_trigger();
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS ops.retry_plans (
  id bigserial PRIMARY KEY,
  plan_date date NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  plan jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  executed_at timestamptz,
  result jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT retry_plans_status_chk CHECK (status IN ('pending', 'running', 'ok', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS retry_plans_date_status_idx
  ON ops.retry_plans(plan_date DESC, status);

GRANT USAGE ON SCHEMA ops TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA ops TO finance_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA ops GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA ops GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;

COMMIT;
