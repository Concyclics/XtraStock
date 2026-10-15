CREATE SCHEMA IF NOT EXISTS staging;

DROP TABLE IF EXISTS staging.peoples_daily_articles;

CREATE TABLE staging.peoples_daily_articles (
  external_id text NOT NULL,
  source_type text NOT NULL,
  source_name text NOT NULL,
  title text,
  author text,
  publisher text,
  published_at date,
  url text,
  language text,
  content_text text,
  raw_text text,
  content_hash text,
  newspaper_date date,
  page_no text,
  article_no text,
  page_name text,
  page_url text,
  page_pdf_url text,
  page_image_url text,
  section_name text,
  original_file_path text,
  size_bytes bigint,
  metadata jsonb
);
