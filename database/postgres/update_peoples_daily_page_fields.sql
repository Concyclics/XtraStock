BEGIN;

UPDATE news.articles AS a
SET
  page_name = NULLIF(s.page_name, ''),
  page_url = NULLIF(s.page_url, ''),
  page_pdf_url = NULLIF(s.page_pdf_url, ''),
  page_image_url = NULLIF(s.page_image_url, ''),
  section_name = COALESCE(NULLIF(s.section_name, ''), NULLIF(s.page_name, ''), a.section_name),
  metadata = COALESCE(a.metadata, '{}'::jsonb) || jsonb_strip_nulls(jsonb_build_object(
    'page_name', NULLIF(s.page_name, ''),
    'page_url', NULLIF(s.page_url, ''),
    'page_pdf_url', NULLIF(s.page_pdf_url, ''),
    'page_image_url', NULLIF(s.page_image_url, '')
  ))
FROM staging.peoples_daily_articles AS s
JOIN corpus.documents AS d
  ON d.source_type = s.source_type
 AND d.source_name = s.source_name
 AND d.external_id = s.external_id
WHERE a.document_id = d.id
  AND s.source_name = 'peoples_daily';

ANALYZE news.articles;

COMMIT;

SELECT
  count(*) AS article_rows,
  count(page_name) AS page_name_rows,
  count(page_url) AS page_url_rows
FROM news.articles
WHERE source_name = 'peoples_daily';
