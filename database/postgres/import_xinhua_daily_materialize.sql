BEGIN;

WITH upserted_documents AS (
  INSERT INTO corpus.documents (
    source_type,
    source_name,
    external_id,
    title,
    subtitle,
    author,
    publisher,
    published_at,
    url,
    language,
    content_text,
    raw_text,
    metadata,
    content_hash,
    ingest_status,
    updated_at
  )
  SELECT
    source_type,
    source_name,
    external_id,
    NULLIF(title, ''),
    NULLIF(subtitle, ''),
    NULLIF(author, ''),
    NULLIF(publisher, ''),
    published_at::timestamp AT TIME ZONE 'Asia/Shanghai',
    NULLIF(url, ''),
    COALESCE(NULLIF(language, ''), 'zh'),
    NULLIF(content_text, ''),
    raw_text,
    COALESCE(metadata, '{}'::jsonb),
    content_hash,
    'ok',
    now()
  FROM staging.xinhua_daily_articles
  ON CONFLICT (source_type, source_name, external_id)
  DO UPDATE SET
    title = EXCLUDED.title,
    subtitle = EXCLUDED.subtitle,
    author = EXCLUDED.author,
    publisher = EXCLUDED.publisher,
    published_at = EXCLUDED.published_at,
    url = EXCLUDED.url,
    language = EXCLUDED.language,
    content_text = EXCLUDED.content_text,
    raw_text = EXCLUDED.raw_text,
    metadata = EXCLUDED.metadata,
    content_hash = EXCLUDED.content_hash,
    ingest_status = EXCLUDED.ingest_status,
    updated_at = now()
  WHERE ROW(
    corpus.documents.title, corpus.documents.subtitle, corpus.documents.author,
    corpus.documents.publisher, corpus.documents.published_at, corpus.documents.url,
    corpus.documents.language, corpus.documents.content_text, corpus.documents.raw_text,
    corpus.documents.metadata, corpus.documents.content_hash, corpus.documents.ingest_status
  ) IS DISTINCT FROM ROW(
    EXCLUDED.title, EXCLUDED.subtitle, EXCLUDED.author, EXCLUDED.publisher,
    EXCLUDED.published_at, EXCLUDED.url, EXCLUDED.language, EXCLUDED.content_text,
    EXCLUDED.raw_text, EXCLUDED.metadata, EXCLUDED.content_hash, EXCLUDED.ingest_status
  )
  RETURNING id, source_type, source_name, external_id
),
all_documents AS (
  SELECT d.id, s.*
  FROM staging.xinhua_daily_articles s
  JOIN corpus.documents d
    ON d.source_type = s.source_type
   AND d.source_name = s.source_name
   AND d.external_id = s.external_id
)
INSERT INTO news.articles (
  document_id,
  source_name,
  newspaper_date,
  page_no,
  article_no,
  page_name,
  page_url,
  page_pdf_url,
  page_image_url,
  section_name,
  author,
  original_file_path,
  topic,
  region,
  metadata
)
SELECT
  id,
  source_name,
  newspaper_date,
  NULLIF(page_no, ''),
  NULLIF(article_no, ''),
  NULLIF(page_name, ''),
  NULLIF(page_url, ''),
  NULLIF(page_pdf_url, ''),
  NULLIF(page_image_url, ''),
  COALESCE(NULLIF(section_name, ''), NULLIF(page_name, '')),
  NULLIF(author, ''),
  original_file_path,
  NULL,
  NULL,
  COALESCE(metadata, '{}'::jsonb)
FROM all_documents
ON CONFLICT (document_id)
DO UPDATE SET
  source_name = EXCLUDED.source_name,
  newspaper_date = EXCLUDED.newspaper_date,
  page_no = EXCLUDED.page_no,
  article_no = EXCLUDED.article_no,
  page_name = EXCLUDED.page_name,
  page_url = EXCLUDED.page_url,
  page_pdf_url = EXCLUDED.page_pdf_url,
  page_image_url = EXCLUDED.page_image_url,
  section_name = EXCLUDED.section_name,
  author = EXCLUDED.author,
  original_file_path = EXCLUDED.original_file_path,
  metadata = EXCLUDED.metadata
WHERE ROW(
  news.articles.source_name, news.articles.newspaper_date, news.articles.page_no,
  news.articles.article_no, news.articles.page_name, news.articles.page_url,
  news.articles.page_pdf_url, news.articles.page_image_url, news.articles.section_name,
  news.articles.author, news.articles.original_file_path, news.articles.metadata
) IS DISTINCT FROM ROW(
  EXCLUDED.source_name, EXCLUDED.newspaper_date, EXCLUDED.page_no, EXCLUDED.article_no,
  EXCLUDED.page_name, EXCLUDED.page_url, EXCLUDED.page_pdf_url, EXCLUDED.page_image_url,
  EXCLUDED.section_name, EXCLUDED.author, EXCLUDED.original_file_path, EXCLUDED.metadata
);

WITH all_documents AS (
  SELECT d.id, s.*
  FROM staging.xinhua_daily_articles s
  JOIN corpus.documents d
    ON d.source_type = s.source_type
   AND d.source_name = s.source_name
   AND d.external_id = s.external_id
)
INSERT INTO corpus.document_files (
  document_id,
  file_role,
  file_path,
  file_hash,
  mime_type,
  size_bytes,
  ocr_status,
  ocr_quality
)
SELECT
  id,
  'raw_txt',
  original_file_path,
  content_hash,
  'text/plain; charset=utf-8',
  size_bytes,
  'none',
  '{}'::jsonb
FROM all_documents
WHERE NULLIF(original_file_path, '') IS NOT NULL
ON CONFLICT (document_id, file_role, file_path)
DO UPDATE SET
  file_hash = EXCLUDED.file_hash,
  mime_type = EXCLUDED.mime_type,
  size_bytes = EXCLUDED.size_bytes
WHERE ROW(
  corpus.document_files.file_hash, corpus.document_files.mime_type,
  corpus.document_files.size_bytes
) IS DISTINCT FROM ROW(
  EXCLUDED.file_hash, EXCLUDED.mime_type, EXCLUDED.size_bytes
);

WITH all_documents AS (
  SELECT d.id, s.*
  FROM staging.xinhua_daily_articles s
  JOIN corpus.documents d
    ON d.source_type = s.source_type
   AND d.source_name = s.source_name
   AND d.external_id = s.external_id
)
INSERT INTO corpus.document_files (
  document_id,
  file_role,
  file_path,
  file_hash,
  mime_type,
  size_bytes,
  ocr_status,
  ocr_quality
)
SELECT
  id,
  'html',
  raw_html_file_path,
  NULL,
  'text/html; charset=utf-8',
  raw_html_size_bytes,
  'none',
  '{}'::jsonb
FROM all_documents
WHERE NULLIF(raw_html_file_path, '') IS NOT NULL
ON CONFLICT (document_id, file_role, file_path)
DO UPDATE SET
  mime_type = EXCLUDED.mime_type,
  size_bytes = EXCLUDED.size_bytes
WHERE ROW(
  corpus.document_files.mime_type, corpus.document_files.size_bytes
) IS DISTINCT FROM ROW(EXCLUDED.mime_type, EXCLUDED.size_bytes);

COMMIT;

-- Statistics refresh must not retain the import transaction snapshot.
ANALYZE corpus.documents;
ANALYZE news.articles;
ANALYZE corpus.document_files;

SELECT
  (SELECT count(*) FROM staging.xinhua_daily_articles) AS staging_rows,
  (SELECT count(*) FROM corpus.documents WHERE source_type = 'news' AND source_name = 'xinhua_daily') AS document_rows,
  (SELECT count(*) FROM news.articles WHERE source_name = 'xinhua_daily') AS article_rows,
  (SELECT count(*) FROM corpus.document_files f JOIN corpus.documents d ON d.id = f.document_id WHERE d.source_type = 'news' AND d.source_name = 'xinhua_daily' AND f.file_role = 'raw_txt') AS raw_txt_file_rows,
  (SELECT count(*) FROM corpus.document_files f JOIN corpus.documents d ON d.id = f.document_id WHERE d.source_type = 'news' AND d.source_name = 'xinhua_daily' AND f.file_role = 'html') AS html_file_rows;
