BEGIN;

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
  d.id,
  s.source_name,
  s.newspaper_date,
  NULLIF(s.page_no, ''),
  NULLIF(s.article_no, ''),
  NULLIF(s.page_name, ''),
  NULLIF(s.page_url, ''),
  NULLIF(s.page_pdf_url, ''),
  NULLIF(s.page_image_url, ''),
  COALESCE(NULLIF(s.section_name, ''), NULLIF(s.page_name, '')),
  NULLIF(s.author, ''),
  s.original_file_path,
  NULL,
  NULL,
  COALESCE(s.metadata, '{}'::jsonb)
FROM staging.peoples_daily_articles s
JOIN corpus.documents d
  ON d.source_type = s.source_type
 AND d.source_name = s.source_name
 AND d.external_id = s.external_id
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
  metadata = EXCLUDED.metadata;

ANALYZE news.articles;

COMMIT;

SELECT
  (SELECT count(*) FROM staging.peoples_daily_articles) AS staging_rows,
  (SELECT count(*) FROM corpus.documents WHERE source_type = 'peoples_daily' AND source_name = 'peoples_daily') AS document_rows,
  (SELECT count(*) FROM news.articles WHERE source_name = 'peoples_daily') AS article_rows,
  (SELECT count(*) FROM corpus.document_files f JOIN corpus.documents d ON d.id = f.document_id WHERE d.source_type = 'peoples_daily' AND f.file_role = 'raw_txt') AS raw_txt_file_rows;
