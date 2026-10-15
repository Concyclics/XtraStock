BEGIN;

CREATE TABLE IF NOT EXISTS corpus.document_embeddings_v2 (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  chunk_id uuid REFERENCES corpus.document_chunks(id) ON DELETE CASCADE,
  embedding_model text NOT NULL,
  embedding vector(1024) NOT NULL,
  dimensions integer NOT NULL DEFAULT 1024 CHECK (dimensions = 1024),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_embeddings_v2_target_uniq
    UNIQUE (document_id, chunk_id, embedding_model)
);

CREATE INDEX IF NOT EXISTS document_embeddings_v2_document_idx
  ON corpus.document_embeddings_v2 (document_id);
CREATE INDEX IF NOT EXISTS document_embeddings_v2_chunk_idx
  ON corpus.document_embeddings_v2 (chunk_id);
CREATE INDEX IF NOT EXISTS document_embeddings_v2_model_idx
  ON corpus.document_embeddings_v2 (embedding_model, created_at DESC);
CREATE INDEX IF NOT EXISTS document_embeddings_v2_hnsw_cosine_idx
  ON corpus.document_embeddings_v2 USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

COMMENT ON TABLE corpus.document_embeddings_v2 IS
  'OpenAI-compatible remote embeddings at the provider native 1024 dimensions.';

GRANT SELECT, INSERT, UPDATE, DELETE ON corpus.document_embeddings_v2 TO finance_app;
GRANT SELECT ON corpus.document_embeddings_v2 TO finance_ai_reader;

COMMIT;
