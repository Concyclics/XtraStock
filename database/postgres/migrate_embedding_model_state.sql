BEGIN;
CREATE TABLE IF NOT EXISTS ops.document_embedding_model_state (
 document_id uuid NOT NULL REFERENCES corpus.documents(id),embedding_model text NOT NULL,
 source_updated_at timestamptz,status text NOT NULL,retry_count int NOT NULL DEFAULT 0,
 content_sha256 text,last_error text,updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(document_id,embedding_model)
);
GRANT SELECT,INSERT,UPDATE ON ops.document_embedding_model_state TO finance_app;
GRANT SELECT ON ops.document_embedding_model_state TO finance_ai_reader;
COMMIT;
