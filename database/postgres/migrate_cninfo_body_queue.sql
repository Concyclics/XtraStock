BEGIN;
CREATE TABLE IF NOT EXISTS ops.cninfo_body_queue (
  document_id uuid PRIMARY KEY REFERENCES corpus.documents(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','ok','failed','needs_ocr','unavailable')),
  attempts integer NOT NULL DEFAULT 0,
  started_at timestamptz,
  finished_at timestamptz,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  body_chars integer,
  text_sha256 text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS cninfo_body_queue_pending_idx ON ops.cninfo_body_queue(status,next_attempt_at);
GRANT SELECT,INSERT,UPDATE,DELETE ON ops.cninfo_body_queue TO finance_app;
COMMIT;
