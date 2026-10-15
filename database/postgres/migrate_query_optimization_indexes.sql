\echo 'Applying query optimization indexes'

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- MMClaw document tools first narrow by source and recent publication time.
CREATE INDEX IF NOT EXISTS documents_source_published_at_idx
  ON corpus.documents (source_type, published_at DESC);

-- Keyword/event evidence searches use contains-style ILIKE patterns.
CREATE INDEX IF NOT EXISTS document_keywords_keyword_trgm_idx
  ON corpus.document_keywords USING gin (keyword gin_trgm_ops);

CREATE INDEX IF NOT EXISTS document_events_summary_trgm_idx
  ON analytics.document_events USING gin (summary gin_trgm_ops);

CREATE INDEX IF NOT EXISTS document_events_evidence_trgm_idx
  ON analytics.document_events USING gin (evidence gin_trgm_ops);

CREATE INDEX IF NOT EXISTS document_events_type_date_idx
  ON analytics.document_events (event_type, event_date DESC);

-- Common MMClaw joins and optional fuzzy stock/report filters.
CREATE INDEX IF NOT EXISTS report_ratings_document_idx
  ON research.report_ratings (document_id);

CREATE INDEX IF NOT EXISTS reports_stock_name_trgm_idx
  ON research.reports USING gin (stock_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS reports_title_trgm_idx
  ON research.reports USING gin (report_title gin_trgm_ops);

CREATE INDEX IF NOT EXISTS news_articles_source_date_idx
  ON news.articles (source_name, newspaper_date DESC);

-- Knowledge-graph/theme tools use ILIKE for concept and subindustry matching.
CREATE INDEX IF NOT EXISTS concepts_name_trgm_idx
  ON kg.concepts USING gin (name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ai_hardware_subindustry_daily_subindustry_trgm_idx
  ON analytics.ai_hardware_subindustry_daily USING gin (subindustry gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ai_hardware_asset_exposure_stock_name_trgm_idx
  ON analytics.ai_hardware_asset_exposure USING gin (stock_name gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ai_hardware_asset_exposure_subindustry_trgm_idx
  ON analytics.ai_hardware_asset_exposure USING gin (subindustry gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ai_hardware_terms_term_trgm_idx
  ON analytics.ai_hardware_terms USING gin (term gin_trgm_ops);

CREATE INDEX IF NOT EXISTS ai_hardware_terms_normalized_term_trgm_idx
  ON analytics.ai_hardware_terms USING gin (normalized_term gin_trgm_ops);

ANALYZE corpus.documents;
ANALYZE corpus.document_keywords;
ANALYZE analytics.document_events;
ANALYZE research.reports;
ANALYZE research.report_ratings;
ANALYZE news.articles;
ANALYZE kg.concepts;
ANALYZE analytics.ai_hardware_subindustry_daily;
ANALYZE analytics.ai_hardware_asset_exposure;
ANALYZE analytics.ai_hardware_terms;
