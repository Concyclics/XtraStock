BEGIN;

ALTER TABLE corpus.document_embeddings
  ALTER COLUMN embedding TYPE vector(512)
  USING embedding::vector(512);

CREATE INDEX IF NOT EXISTS document_embeddings_model_idx
  ON corpus.document_embeddings (embedding_model, created_at DESC);
CREATE INDEX IF NOT EXISTS document_embeddings_hnsw_cosine_idx
  ON corpus.document_embeddings USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS ops.document_embedding_queue (
  document_id uuid PRIMARY KEY REFERENCES corpus.documents(id) ON DELETE CASCADE,
  source_updated_at timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  retry_count integer NOT NULL DEFAULT 0,
  dirty_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  processed_at timestamptz,
  embedding_model text,
  chunk_count integer,
  last_error text,
  CONSTRAINT document_embedding_queue_status_chk
    CHECK (status IN ('pending','running','ok','failed','skipped'))
);

CREATE INDEX IF NOT EXISTS document_embedding_queue_status_dirty_idx
  ON ops.document_embedding_queue (status, dirty_at, document_id);

CREATE OR REPLACE FUNCTION ops.enqueue_document_embedding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  INSERT INTO ops.document_embedding_queue (
    document_id, source_updated_at, status, retry_count, dirty_at,
    started_at, processed_at, embedding_model, chunk_count, last_error
  )
  VALUES (
    NEW.id, NEW.updated_at, 'pending', 0, now(),
    NULL, NULL, NULL, NULL, NULL
  )
  ON CONFLICT (document_id) DO UPDATE SET
    source_updated_at = EXCLUDED.source_updated_at,
    status = 'pending', retry_count = 0, dirty_at = now(),
    started_at = NULL, processed_at = NULL, chunk_count = NULL, last_error = NULL
  WHERE ops.document_embedding_queue.source_updated_at IS DISTINCT FROM EXCLUDED.source_updated_at;
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS corpus_documents_embedding_queue_trg ON corpus.documents;
CREATE TRIGGER corpus_documents_embedding_queue_trg
AFTER INSERT OR UPDATE OF title, content_text, content_md, raw_text, updated_at
ON corpus.documents
FOR EACH ROW EXECUTE FUNCTION ops.enqueue_document_embedding();

INSERT INTO ops.document_embedding_queue (document_id, source_updated_at, status, dirty_at)
SELECT d.id, d.updated_at, 'pending', now()
FROM corpus.documents d
LEFT JOIN ops.document_embedding_queue q ON q.document_id=d.id
WHERE q.document_id IS NULL
ON CONFLICT (document_id) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON corpus.document_chunks, corpus.document_embeddings TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ops.document_embedding_queue TO finance_app;
GRANT SELECT ON corpus.document_chunks, corpus.document_embeddings, ops.document_embedding_queue TO finance_ai_reader;

INSERT INTO agent.metric_catalog (
  metric_key, display_name, description, canonical_relation, value_columns,
  grain, time_column, available_at_column, dimension_columns, unit, formula,
  freshness_slo, null_policy, lookahead_risk, status, metadata
)
VALUES (
  'document_embedding', '文档语义向量',
  '中文金融文档分块的本地BGE 512维向量，用于混合语义召回。',
  'corpus.document_embeddings', ARRAY['embedding'], '一个文档分块一个模型向量',
  'created_at', 'created_at', ARRAY['document_id','chunk_id','embedding_model'],
  'cosine_vector', NULL, INTERVAL '2 hours',
  '仅已完成分块和embedding的文档进入语义召回；必须披露索引覆盖率',
  'none', 'active',
  '{"model":"BAAI/bge-small-zh-v1.5","dimensions":512,"retrieval":"hybrid_rrf"}'::jsonb
)
ON CONFLICT (metric_key) DO UPDATE SET
  description=EXCLUDED.description, canonical_relation=EXCLUDED.canonical_relation,
  value_columns=EXCLUDED.value_columns, grain=EXCLUDED.grain,
  time_column=EXCLUDED.time_column, available_at_column=EXCLUDED.available_at_column,
  dimension_columns=EXCLUDED.dimension_columns, unit=EXCLUDED.unit,
  freshness_slo=EXCLUDED.freshness_slo, null_policy=EXCLUDED.null_policy,
  metadata=EXCLUDED.metadata, status=EXCLUDED.status, updated_at=now();

INSERT INTO agent.data_contracts (
  dataset_key, relation_name, grain, primary_key_columns, watermark_column,
  freshness_slo, required_columns, quality_checks
)
VALUES (
  'document_embeddings', 'corpus.document_embeddings', '一个文档分块一个模型向量',
  ARRAY['document_id','chunk_id','embedding_model'], 'created_at', INTERVAL '2 hours',
  ARRAY['document_id','chunk_id','embedding_model','embedding','dimensions'],
  '["dimensions必须为512","chunk和embedding一一对应","队列source_updated_at不得落后文档updated_at","混合检索必须披露语义覆盖率"]'::jsonb
)
ON CONFLICT (dataset_key) DO UPDATE SET
  relation_name=EXCLUDED.relation_name, grain=EXCLUDED.grain,
  primary_key_columns=EXCLUDED.primary_key_columns, watermark_column=EXCLUDED.watermark_column,
  freshness_slo=EXCLUDED.freshness_slo, required_columns=EXCLUDED.required_columns,
  quality_checks=EXCLUDED.quality_checks, updated_at=now();

COMMIT;
