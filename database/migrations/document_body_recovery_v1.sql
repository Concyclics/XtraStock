CREATE TABLE IF NOT EXISTS ops.document_body_recovery (
 document_id uuid PRIMARY KEY REFERENCES corpus.documents(id),status text NOT NULL,
 attempts integer NOT NULL DEFAULT 0,source_url text,source_sha256 text,body_sha256 text,
 page_count integer,pages_with_text integer,last_error text,
 started_at timestamptz,finished_at timestamptz,updated_at timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT,INSERT,UPDATE ON ops.document_body_recovery TO finance_app;
