CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS corpus.document_search_index (
  document_id uuid PRIMARY KEY REFERENCES corpus.documents(id) ON DELETE CASCADE,
  source_type text NOT NULL,
  source_name text NOT NULL,
  external_id text,
  published_at timestamptz,
  title text,
  url text,
  keywords text[] NOT NULL DEFAULT '{}'::text[],
  stock_codes text[] NOT NULL DEFAULT '{}'::text[],
  industries text[] NOT NULL DEFAULT '{}'::text[],
  event_types text[] NOT NULL DEFAULT '{}'::text[],
  search_text text NOT NULL DEFAULT '',
  search_vector tsvector NOT NULL DEFAULT ''::tsvector,
  body_chars integer NOT NULL DEFAULT 0,
  indexed_at timestamptz NOT NULL DEFAULT now(),
  source_updated_at timestamptz,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE corpus.document_search_index IS 'Denormalized search index for finance documents. Used as PostgreSQL FTS fallback and as the source of Meilisearch documents.';
COMMENT ON COLUMN corpus.document_search_index.search_text IS 'Weighted plain text assembled from title, keywords, events, research metadata, and document body.';
COMMENT ON COLUMN corpus.document_search_index.search_vector IS 'PostgreSQL full-text vector built with the simple configuration.';

CREATE INDEX IF NOT EXISTS document_search_vector_idx
  ON corpus.document_search_index USING gin (search_vector);

CREATE INDEX IF NOT EXISTS document_search_published_idx
  ON corpus.document_search_index (published_at DESC);

CREATE INDEX IF NOT EXISTS document_search_source_published_idx
  ON corpus.document_search_index (source_type, published_at DESC);

CREATE INDEX IF NOT EXISTS document_search_keywords_idx
  ON corpus.document_search_index USING gin (keywords);

CREATE INDEX IF NOT EXISTS document_search_stock_codes_idx
  ON corpus.document_search_index USING gin (stock_codes);

CREATE INDEX IF NOT EXISTS document_search_industries_idx
  ON corpus.document_search_index USING gin (industries);

CREATE INDEX IF NOT EXISTS document_search_title_trgm_idx
  ON corpus.document_search_index USING gin (title gin_trgm_ops);

CREATE INDEX IF NOT EXISTS document_search_text_trgm_idx
  ON corpus.document_search_index USING gin (search_text gin_trgm_ops);
