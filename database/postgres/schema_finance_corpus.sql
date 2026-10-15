BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE SCHEMA IF NOT EXISTS corpus;
CREATE SCHEMA IF NOT EXISTS research;
CREATE SCHEMA IF NOT EXISTS news;
CREATE SCHEMA IF NOT EXISTS kg;
CREATE SCHEMA IF NOT EXISTS events;
CREATE SCHEMA IF NOT EXISTS ai;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS analytics;

COMMENT ON SCHEMA corpus IS 'Unified document corpus for reports, OCR text, news, source fetches, chunks, embeddings, and keywords.';
COMMENT ON SCHEMA research IS 'Research-report-specific metadata, ratings, and financial forecasts.';
COMMENT ON SCHEMA news IS 'News article metadata, including People''s Daily and future news sources.';
COMMENT ON SCHEMA kg IS 'Normalized entities and document-entity mentions.';
COMMENT ON SCHEMA events IS 'Structured events extracted from documents and market/news feeds.';
COMMENT ON SCHEMA ai IS 'LLM extraction runs and raw/validated model outputs.';
COMMENT ON SCHEMA market IS 'Assets and time-series market data, backed by TimescaleDB where available.';
COMMENT ON SCHEMA analytics IS 'Derived analysis outputs, aggregates, and agent-facing materializations.';

CREATE TABLE IF NOT EXISTS corpus.documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_type text NOT NULL,
  source_name text NOT NULL,
  external_id text,
  title text,
  subtitle text,
  author text,
  publisher text,
  published_at timestamptz,
  url text,
  language text NOT NULL DEFAULT 'zh',
  content_text text,
  content_md text,
  raw_text text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  content_hash text,
  ingest_status text NOT NULL DEFAULT 'pending',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT documents_ingest_status_chk CHECK (
    ingest_status IN ('pending', 'ok', 'failed', 'skipped')
  ),
  CONSTRAINT documents_source_identity_uniq UNIQUE (source_type, source_name, external_id)
);

COMMENT ON TABLE corpus.documents IS 'Canonical document table. Every report, OCR result, People''s Daily article, and future news article should have one row here.';
COMMENT ON COLUMN corpus.documents.source_type IS 'Document category: research_report, peoples_daily, news, announcement, etc.';
COMMENT ON COLUMN corpus.documents.source_name IS 'Source system or crawler name: eastmoney, peoples_daily, cls, sina, etc.';
COMMENT ON COLUMN corpus.documents.external_id IS 'Stable source-side ID, such as Eastmoney infocode or People''s Daily file stem.';
COMMENT ON COLUMN corpus.documents.content_text IS 'Clean plain text used by search, summarization, and agent analysis.';
COMMENT ON COLUMN corpus.documents.content_md IS 'Markdown content, mainly OCR markdown or structured article markdown.';
COMMENT ON COLUMN corpus.documents.raw_text IS 'Unmodified text from source page or source file.';
COMMENT ON COLUMN corpus.documents.metadata IS 'Source-specific fields that do not deserve first-class columns yet.';

CREATE INDEX IF NOT EXISTS documents_published_at_idx ON corpus.documents (published_at DESC);
CREATE INDEX IF NOT EXISTS documents_source_idx ON corpus.documents (source_type, source_name);
CREATE INDEX IF NOT EXISTS documents_title_trgm_idx ON corpus.documents USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS documents_metadata_gin_idx ON corpus.documents USING gin (metadata);
CREATE INDEX IF NOT EXISTS documents_updated_published_idx
  ON corpus.documents (updated_at, published_at);

CREATE TABLE IF NOT EXISTS corpus.document_files (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  file_role text NOT NULL,
  file_path text NOT NULL,
  file_hash text,
  mime_type text,
  size_bytes bigint,
  page_count integer,
  ocr_status text NOT NULL DEFAULT 'none',
  ocr_quality jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_files_role_chk CHECK (
    file_role IN ('pdf', 'ocr_md', 'ocr_quality_json', 'ocr_quality_md', 'raw_txt', 'html', 'other')
  ),
  CONSTRAINT document_files_ocr_status_chk CHECK (
    ocr_status IN ('none', 'pending', 'ok', 'partial', 'failed')
  ),
  CONSTRAINT document_files_doc_role_path_uniq UNIQUE (document_id, file_role, file_path)
);

COMMENT ON TABLE corpus.document_files IS 'Files attached to a document: PDFs, OCR markdown, quality reports, raw TXT, or HTML.';
CREATE INDEX IF NOT EXISTS document_files_document_idx ON corpus.document_files (document_id);
CREATE INDEX IF NOT EXISTS document_files_path_idx ON corpus.document_files (file_path);

CREATE TABLE IF NOT EXISTS corpus.source_fetches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_name text NOT NULL,
  external_id text,
  url text,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  http_status integer,
  raw_payload jsonb,
  raw_text text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE corpus.source_fetches IS 'Raw fetch audit log. Use this to debug crawler failures, 404 skips, retries, and source changes.';
CREATE INDEX IF NOT EXISTS source_fetches_source_idx ON corpus.source_fetches (source_name, external_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS source_fetches_http_status_idx ON corpus.source_fetches (http_status);

CREATE TABLE IF NOT EXISTS corpus.document_chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  chunk_index integer NOT NULL,
  chunk_text text NOT NULL,
  token_count integer,
  char_count integer,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_chunks_doc_index_uniq UNIQUE (document_id, chunk_index)
);

COMMENT ON TABLE corpus.document_chunks IS 'Chunked text for retrieval, embeddings, and long-document LLM processing.';
CREATE INDEX IF NOT EXISTS document_chunks_document_idx ON corpus.document_chunks (document_id);
CREATE INDEX IF NOT EXISTS document_chunks_text_trgm_idx ON corpus.document_chunks USING gin (chunk_text gin_trgm_ops);

CREATE TABLE IF NOT EXISTS corpus.document_embeddings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  chunk_id uuid REFERENCES corpus.document_chunks(id) ON DELETE CASCADE,
  embedding_model text NOT NULL,
  embedding vector,
  dimensions integer,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_embeddings_target_uniq UNIQUE (document_id, chunk_id, embedding_model)
);

COMMENT ON TABLE corpus.document_embeddings IS 'pgvector embeddings for whole documents or chunks. Use chunk_id NULL for document-level vectors.';
CREATE INDEX IF NOT EXISTS document_embeddings_document_idx ON corpus.document_embeddings (document_id);
CREATE INDEX IF NOT EXISTS document_embeddings_chunk_idx ON corpus.document_embeddings (chunk_id);

CREATE TABLE IF NOT EXISTS corpus.document_keywords (
  id bigserial PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  keyword text NOT NULL,
  weight numeric,
  method text NOT NULL DEFAULT 'manual',
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_keywords_uniq UNIQUE (document_id, keyword, method)
);

COMMENT ON TABLE corpus.document_keywords IS 'Keywords and topics produced by rules, TF-IDF, LLM extraction, or manual curation.';
CREATE INDEX IF NOT EXISTS document_keywords_keyword_idx ON corpus.document_keywords (keyword);
CREATE INDEX IF NOT EXISTS document_keywords_document_idx ON corpus.document_keywords (document_id);

CREATE TABLE IF NOT EXISTS research.reports (
  document_id uuid PRIMARY KEY REFERENCES corpus.documents(id) ON DELETE CASCADE,
  report_type text NOT NULL,
  eastmoney_infocode text UNIQUE,
  report_date date,
  report_title text,
  institution text,
  analyst_names text[] NOT NULL DEFAULT '{}'::text[],
  rating text,
  rating_change text,
  industry_name text,
  stock_code text,
  stock_name text,
  source_url text,
  web_text_status text,
  pdf_status text NOT NULL DEFAULT 'missing',
  ocr_status text NOT NULL DEFAULT 'pending',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT reports_type_chk CHECK (report_type IN ('stock', 'industry', 'macro', 'strategy', 'other')),
  CONSTRAINT reports_pdf_status_chk CHECK (pdf_status IN ('missing', 'downloaded', 'failed')),
  CONSTRAINT reports_ocr_status_chk CHECK (ocr_status IN ('pending', 'ok', 'partial', 'failed', 'none'))
);

COMMENT ON TABLE research.reports IS 'Business metadata for research reports. One row maps to one corpus.documents row.';
COMMENT ON COLUMN research.reports.rating IS 'Source or extracted rating, such as 买入, 增持, 中性, 未评级.';
COMMENT ON COLUMN research.reports.rating_change IS 'Rating change, such as 维持, 调高, 调低, 首次.';
CREATE INDEX IF NOT EXISTS reports_date_idx ON research.reports (report_date DESC);
CREATE INDEX IF NOT EXISTS reports_stock_idx ON research.reports (stock_code, report_date DESC);
CREATE INDEX IF NOT EXISTS reports_industry_idx ON research.reports (industry_name, report_date DESC);
CREATE INDEX IF NOT EXISTS reports_rating_idx ON research.reports (rating, rating_change);
CREATE INDEX IF NOT EXISTS reports_institution_idx ON research.reports (institution);
CREATE INDEX IF NOT EXISTS reports_updated_date_idx
  ON research.reports (updated_at, report_date);

CREATE TABLE IF NOT EXISTS research.report_forecasts (
  id bigserial PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES research.reports(document_id) ON DELETE CASCADE,
  forecast_year integer NOT NULL,
  revenue numeric,
  net_profit numeric,
  eps numeric,
  pe numeric,
  currency text DEFAULT 'CNY',
  source text NOT NULL DEFAULT 'unknown',
  confidence numeric,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT report_forecasts_uniq UNIQUE (document_id, forecast_year, source)
);

COMMENT ON TABLE research.report_forecasts IS 'Normalized earnings/valuation forecasts. Use narrow rows instead of year-specific columns.';
CREATE INDEX IF NOT EXISTS report_forecasts_document_idx ON research.report_forecasts (document_id);
CREATE INDEX IF NOT EXISTS report_forecasts_year_idx ON research.report_forecasts (forecast_year);

CREATE TABLE IF NOT EXISTS research.report_ratings (
  id bigserial PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES research.reports(document_id) ON DELETE CASCADE,
  stock_code text,
  rating text,
  rating_change text,
  previous_rating text,
  target_price numeric,
  extracted_by text NOT NULL DEFAULT 'source',
  confidence numeric,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE research.report_ratings IS 'Rating facts for report-level or stock-level recommendation analysis.';
CREATE INDEX IF NOT EXISTS report_ratings_stock_idx ON research.report_ratings (stock_code);
CREATE INDEX IF NOT EXISTS report_ratings_rating_idx ON research.report_ratings (rating, rating_change);

CREATE TABLE IF NOT EXISTS news.articles (
  document_id uuid PRIMARY KEY REFERENCES corpus.documents(id) ON DELETE CASCADE,
  source_name text NOT NULL,
  newspaper_date date,
  page_no text,
  article_no text,
  section_name text,
  author text,
  original_file_path text,
  topic text,
  region text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE news.articles IS 'News article metadata. People''s Daily TXT files and future news crawlers should write here.';
CREATE INDEX IF NOT EXISTS news_articles_date_idx ON news.articles (newspaper_date DESC);
CREATE INDEX IF NOT EXISTS news_articles_source_idx ON news.articles (source_name);
CREATE INDEX IF NOT EXISTS news_articles_topic_idx ON news.articles (topic);

CREATE TABLE IF NOT EXISTS kg.entities (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  entity_type text NOT NULL,
  name text NOT NULL,
  normalized_name text,
  external_code text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT entities_identity_uniq UNIQUE (entity_type, normalized_name, external_code)
);

COMMENT ON TABLE kg.entities IS 'Canonical entities: stocks, companies, industries, people, organizations, regions, policies, and products.';
CREATE INDEX IF NOT EXISTS entities_type_name_idx ON kg.entities (entity_type, name);
CREATE INDEX IF NOT EXISTS entities_name_trgm_idx ON kg.entities USING gin (name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS kg.document_entities (
  id bigserial PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  entity_id uuid NOT NULL REFERENCES kg.entities(id) ON DELETE CASCADE,
  mention text,
  role text,
  confidence numeric,
  source text NOT NULL DEFAULT 'manual',
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE kg.document_entities IS 'Mentions and roles linking documents to normalized entities.';
CREATE UNIQUE INDEX IF NOT EXISTS document_entities_unique_role_idx
  ON kg.document_entities (document_id, entity_id, COALESCE(role, ''));
CREATE INDEX IF NOT EXISTS document_entities_entity_idx ON kg.document_entities (entity_id);
CREATE INDEX IF NOT EXISTS document_entities_role_idx ON kg.document_entities (role);

CREATE TABLE IF NOT EXISTS events.events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_type text NOT NULL,
  event_time timestamptz,
  title text,
  summary text,
  source_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  entities jsonb NOT NULL DEFAULT '[]'::jsonb,
  confidence numeric,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE events.events IS 'Structured events extracted from news, research reports, announcements, and market feeds.';
CREATE INDEX IF NOT EXISTS events_time_idx ON events.events (event_time DESC);
CREATE INDEX IF NOT EXISTS events_type_idx ON events.events (event_type);
CREATE INDEX IF NOT EXISTS events_source_document_idx ON events.events (source_document_id);
CREATE INDEX IF NOT EXISTS events_entities_gin_idx ON events.events USING gin (entities);

CREATE TABLE IF NOT EXISTS events.event_entities (
  id bigserial PRIMARY KEY,
  event_id uuid NOT NULL REFERENCES events.events(id) ON DELETE CASCADE,
  entity_id uuid NOT NULL REFERENCES kg.entities(id) ON DELETE CASCADE,
  role text,
  confidence numeric
);

COMMENT ON TABLE events.event_entities IS 'Normalized entity links for structured events.';
CREATE UNIQUE INDEX IF NOT EXISTS event_entities_unique_role_idx
  ON events.event_entities (event_id, entity_id, COALESCE(role, ''));
CREATE INDEX IF NOT EXISTS event_entities_entity_idx ON events.event_entities (entity_id);

CREATE TABLE IF NOT EXISTS ai.extraction_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  model_name text NOT NULL,
  prompt_version text NOT NULL,
  task_type text NOT NULL,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  status text NOT NULL DEFAULT 'running',
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT extraction_runs_status_chk CHECK (
    status IN ('running', 'ok', 'failed', 'partial')
  )
);

COMMENT ON TABLE ai.extraction_runs IS 'One row per LLM/rule extraction batch. Track model, prompt, task, and config.';
CREATE INDEX IF NOT EXISTS extraction_runs_task_idx ON ai.extraction_runs (task_type, started_at DESC);

CREATE TABLE IF NOT EXISTS ai.document_extractions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id uuid NOT NULL REFERENCES ai.extraction_runs(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  extraction_schema text NOT NULL,
  raw_response jsonb,
  parsed_result jsonb,
  confidence numeric,
  status text NOT NULL DEFAULT 'pending',
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_extractions_status_chk CHECK (
    status IN ('pending', 'ok', 'parse_failed', 'validation_failed', 'failed')
  ),
  CONSTRAINT document_extractions_uniq UNIQUE (run_id, document_id, extraction_schema)
);

COMMENT ON TABLE ai.document_extractions IS 'Raw and parsed LLM outputs. Validate here before materializing into business tables.';
CREATE INDEX IF NOT EXISTS document_extractions_document_idx ON ai.document_extractions (document_id);
CREATE INDEX IF NOT EXISTS document_extractions_schema_idx ON ai.document_extractions (extraction_schema);
CREATE INDEX IF NOT EXISTS document_extractions_result_gin_idx ON ai.document_extractions USING gin (parsed_result);

CREATE TABLE IF NOT EXISTS market.assets (
  asset_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_type text NOT NULL,
  symbol text NOT NULL,
  exchange text,
  name text,
  industry_name text,
  listed_date date,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE market.assets IS 'Tradable or reference assets: stocks, indexes, funds, futures, etc.';
CREATE UNIQUE INDEX IF NOT EXISTS assets_identity_uniq
  ON market.assets (asset_type, symbol, COALESCE(exchange, ''));
CREATE INDEX IF NOT EXISTS assets_symbol_idx ON market.assets (symbol);
CREATE INDEX IF NOT EXISTS assets_name_trgm_idx ON market.assets USING gin (name gin_trgm_ops);

CREATE TABLE IF NOT EXISTS market.bars_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  ts timestamptz NOT NULL,
  open double precision,
  high double precision,
  low double precision,
  close double precision,
  volume double precision,
  amount double precision,
  adj_factor double precision,
  source text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, ts, source)
);

COMMENT ON TABLE market.bars_daily IS 'Daily OHLCV bars. Convert to Timescale hypertable when TimescaleDB is available.';
CREATE INDEX IF NOT EXISTS bars_daily_ts_idx ON market.bars_daily (ts DESC);
ALTER TABLE market.bars_daily
  ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE market.bars_daily
  ALTER COLUMN updated_at SET DEFAULT now();
CREATE INDEX IF NOT EXISTS bars_daily_updated_ts_idx
  ON market.bars_daily (updated_at DESC, ts DESC);

CREATE TABLE IF NOT EXISTS market.bars_1m (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  ts timestamptz NOT NULL,
  open double precision,
  high double precision,
  low double precision,
  close double precision,
  volume double precision,
  amount double precision,
  source text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, ts, source)
);

COMMENT ON TABLE market.bars_1m IS 'Minute OHLCV bars. Keep separate from daily bars for retention and compression policies.';
CREATE INDEX IF NOT EXISTS bars_1m_ts_idx ON market.bars_1m (ts DESC);
ALTER TABLE market.bars_1m
  ADD COLUMN IF NOT EXISTS updated_at timestamptz;
ALTER TABLE market.bars_1m
  ALTER COLUMN updated_at SET DEFAULT now();

CREATE TABLE IF NOT EXISTS market.canonical_bars_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  dt date NOT NULL,
  open double precision,
  high double precision,
  low double precision,
  close double precision,
  volume double precision,
  amount double precision,
  source text NOT NULL,
  adjustment text NOT NULL,
  source_ts timestamptz NOT NULL,
  source_updated_at timestamptz NOT NULL,
  is_fallback boolean NOT NULL DEFAULT false,
  available_at timestamptz NOT NULL,
  quality jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, dt),
  CONSTRAINT canonical_bars_daily_adjustment_chk
    CHECK (adjustment IN ('qfq', 'none'))
);

COMMENT ON TABLE market.canonical_bars_daily IS
  'One finalized daily bar per asset and trading date. Eastmoney qfq is preferred; other rows are explicit fallbacks. Volume is normalized to lots.';
CREATE INDEX IF NOT EXISTS canonical_bars_daily_dt_idx
  ON market.canonical_bars_daily (dt DESC, asset_id);
CREATE INDEX IF NOT EXISTS canonical_bars_daily_fallback_idx
  ON market.canonical_bars_daily (dt DESC, source, adjustment)
  WHERE is_fallback;

CREATE TABLE IF NOT EXISTS market.factors_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  ts date NOT NULL,
  factor_name text NOT NULL,
  factor_value numeric,
  source text NOT NULL,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, ts, factor_name, source)
);

COMMENT ON TABLE market.factors_daily IS 'Daily factors and valuation metrics: pe_ttm, pb, turnover, market_cap, pct_chg, etc.';
CREATE INDEX IF NOT EXISTS factors_daily_name_idx ON market.factors_daily (factor_name, ts DESC);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.proname = 'create_hypertable'
      AND n.nspname = 'public'
  ) THEN
    PERFORM public.create_hypertable('market.bars_daily', 'ts', if_not_exists => TRUE);
    PERFORM public.create_hypertable('market.bars_1m', 'ts', if_not_exists => TRUE);
  END IF;
END
$$;

GRANT USAGE ON SCHEMA corpus, research, news, kg, events, ai, market, analytics TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA corpus, research, news, kg, events, ai, market, analytics TO finance_app;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA corpus, research, news, kg, events, ai, market, analytics TO finance_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA corpus GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA research GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA news GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA kg GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA events GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA ai GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA market GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;

ALTER DEFAULT PRIVILEGES IN SCHEMA corpus GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA research GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA news GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA kg GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA events GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA ai GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA market GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO finance_app;

COMMIT;
