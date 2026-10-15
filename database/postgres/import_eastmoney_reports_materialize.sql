BEGIN;

CREATE TEMP TABLE eastmoney_reports_dedup AS
SELECT DISTINCT ON (source_type, source_name, external_id)
  *
FROM staging.eastmoney_reports
ORDER BY
  source_type,
  source_name,
  external_id,
  (content_md IS NOT NULL) DESC,
  (content_text IS NOT NULL) DESC,
  report_date DESC;

INSERT INTO corpus.documents (
  source_type,
  source_name,
  external_id,
  title,
  publisher,
  published_at,
  url,
  language,
  content_text,
  content_md,
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
  NULLIF(publisher, ''),
  published_at::timestamp AT TIME ZONE 'Asia/Shanghai',
  NULLIF(url, ''),
  COALESCE(NULLIF(language, ''), 'zh'),
  NULLIF(content_text, ''),
  NULLIF(content_md, ''),
  NULLIF(raw_text, ''),
  COALESCE(metadata, '{}'::jsonb),
  content_hash,
  'ok',
  now()
FROM eastmoney_reports_dedup
ON CONFLICT (source_type, source_name, external_id)
DO UPDATE SET
  title = EXCLUDED.title,
  publisher = EXCLUDED.publisher,
  published_at = EXCLUDED.published_at,
  url = EXCLUDED.url,
  language = EXCLUDED.language,
  content_text = CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.content_text ELSE EXCLUDED.content_text END,
  content_md = CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.content_md ELSE EXCLUDED.content_md END,
  raw_text = CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.raw_text ELSE EXCLUDED.raw_text END,
  metadata = CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.metadata || EXCLUDED.metadata ELSE EXCLUDED.metadata END,
  content_hash = CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.content_hash ELSE EXCLUDED.content_hash END,
  ingest_status = EXCLUDED.ingest_status,
  updated_at = now()
WHERE ROW(
  corpus.documents.title,
  corpus.documents.publisher,
  corpus.documents.published_at,
  corpus.documents.url,
  corpus.documents.language,
  corpus.documents.content_text,
  corpus.documents.content_md,
  corpus.documents.raw_text,
  corpus.documents.metadata,
  corpus.documents.content_hash,
  corpus.documents.ingest_status
) IS DISTINCT FROM ROW(
  EXCLUDED.title,
  EXCLUDED.publisher,
  EXCLUDED.published_at,
  EXCLUDED.url,
  EXCLUDED.language,
  CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.content_text ELSE EXCLUDED.content_text END,
  CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.content_md ELSE EXCLUDED.content_md END,
  CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.raw_text ELSE EXCLUDED.raw_text END,
  CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.metadata || EXCLUDED.metadata ELSE EXCLUDED.metadata END,
  CASE WHEN (corpus.documents.metadata ? 'body_recovery' AND coalesce(EXCLUDED.content_text,'')='' AND coalesce(EXCLUDED.content_md,'')='' AND coalesce(EXCLUDED.raw_text,'')='') THEN corpus.documents.content_hash ELSE EXCLUDED.content_hash END,
  EXCLUDED.ingest_status
);

INSERT INTO research.reports (
  document_id,
  report_type,
  eastmoney_infocode,
  report_date,
  report_title,
  institution,
  rating,
  rating_change,
  industry_name,
  stock_code,
  stock_name,
  source_url,
  web_text_status,
  pdf_status,
  ocr_status,
  metadata,
  updated_at
)
SELECT
  d.id,
  s.report_type,
  s.external_id,
  s.report_date,
  NULLIF(s.title, ''),
  NULLIF(s.institution, ''),
  NULLIF(s.rating, ''),
  NULLIF(s.rating_change, ''),
  NULLIF(s.industry_name, ''),
  NULLIF(s.stock_code, ''),
  NULLIF(s.stock_name, ''),
  NULLIF(s.source_url, ''),
  NULLIF(s.web_text_status, ''),
  s.pdf_status,
  s.ocr_status,
  COALESCE(s.metadata, '{}'::jsonb),
  now()
FROM eastmoney_reports_dedup s
JOIN corpus.documents d
  ON d.source_type = s.source_type
 AND d.source_name = s.source_name
 AND d.external_id = s.external_id
ON CONFLICT (document_id)
DO UPDATE SET
  report_type = EXCLUDED.report_type,
  eastmoney_infocode = EXCLUDED.eastmoney_infocode,
  report_date = EXCLUDED.report_date,
  report_title = EXCLUDED.report_title,
  institution = EXCLUDED.institution,
  rating = EXCLUDED.rating,
  rating_change = EXCLUDED.rating_change,
  industry_name = EXCLUDED.industry_name,
  stock_code = EXCLUDED.stock_code,
  stock_name = EXCLUDED.stock_name,
  source_url = EXCLUDED.source_url,
  web_text_status = EXCLUDED.web_text_status,
  pdf_status = EXCLUDED.pdf_status,
  ocr_status = EXCLUDED.ocr_status,
  metadata = EXCLUDED.metadata,
  updated_at = now()
WHERE ROW(
  research.reports.report_type,
  research.reports.eastmoney_infocode,
  research.reports.report_date,
  research.reports.report_title,
  research.reports.institution,
  research.reports.rating,
  research.reports.rating_change,
  research.reports.industry_name,
  research.reports.stock_code,
  research.reports.stock_name,
  research.reports.source_url,
  research.reports.web_text_status,
  research.reports.pdf_status,
  research.reports.ocr_status,
  research.reports.metadata
) IS DISTINCT FROM ROW(
  EXCLUDED.report_type,
  EXCLUDED.eastmoney_infocode,
  EXCLUDED.report_date,
  EXCLUDED.report_title,
  EXCLUDED.institution,
  EXCLUDED.rating,
  EXCLUDED.rating_change,
  EXCLUDED.industry_name,
  EXCLUDED.stock_code,
  EXCLUDED.stock_name,
  EXCLUDED.source_url,
  EXCLUDED.web_text_status,
  EXCLUDED.pdf_status,
  EXCLUDED.ocr_status,
  EXCLUDED.metadata
);

INSERT INTO research.report_ratings (
  document_id,
  stock_code,
  rating,
  rating_change,
  extracted_by,
  confidence,
  metadata
)
SELECT
  r.document_id,
  NULLIF(s.stock_code, ''),
  NULLIF(s.rating, ''),
  NULLIF(s.rating_change, ''),
  'eastmoney',
  1.0,
  jsonb_build_object('report_type', s.report_type, 'industry_name', s.industry_name)
FROM eastmoney_reports_dedup s
JOIN research.reports r ON r.eastmoney_infocode = s.external_id
WHERE NULLIF(s.rating, '') IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM research.report_ratings existing
    WHERE existing.document_id = r.document_id
      AND COALESCE(existing.stock_code, '') = COALESCE(NULLIF(s.stock_code, ''), '')
      AND COALESCE(existing.rating, '') = COALESCE(NULLIF(s.rating, ''), '')
      AND COALESCE(existing.rating_change, '') = COALESCE(NULLIF(s.rating_change, ''), '')
      AND existing.extracted_by = 'eastmoney'
  );

INSERT INTO research.report_forecasts (
  document_id,
  forecast_year,
  eps,
  pe,
  source,
  confidence,
  metadata
)
SELECT document_id, forecast_year, eps, pe, 'eastmoney', 1.0, jsonb_build_object('semantic_contract','earnings-forecast-v2','year_verified',true,'year_basis','api.currentYear','eps_unit','CNY/share','pe_unit','times')
FROM (
  SELECT r.document_id, s.forecast_current_year AS forecast_year, s.forecast_current_eps AS eps, s.forecast_current_pe AS pe
  FROM eastmoney_reports_dedup s
  JOIN research.reports r ON r.eastmoney_infocode = s.external_id
  WHERE s.report_type = 'stock' AND s.forecast_current_year IS NOT NULL
  UNION ALL
  SELECT r.document_id, s.forecast_next_year, s.forecast_next_eps, s.forecast_next_pe
  FROM eastmoney_reports_dedup s
  JOIN research.reports r ON r.eastmoney_infocode = s.external_id
  WHERE s.report_type = 'stock' AND s.forecast_next_year IS NOT NULL
  UNION ALL
  SELECT r.document_id, s.forecast_later_year, s.forecast_later_eps, s.forecast_later_pe
  FROM eastmoney_reports_dedup s
  JOIN research.reports r ON r.eastmoney_infocode = s.external_id
  WHERE s.report_type = 'stock' AND s.forecast_later_year IS NOT NULL
) x
WHERE eps IS NOT NULL OR pe IS NOT NULL
ON CONFLICT (document_id, forecast_year, source)
DO UPDATE SET
  net_profit = NULL,
  eps = EXCLUDED.eps,
  pe = EXCLUDED.pe,
  confidence = EXCLUDED.confidence,
  metadata = EXCLUDED.metadata;

-- Dated text supplies historical fiscal years independently of rolling API columns.
INSERT INTO research.report_forecasts(document_id,forecast_year,eps,net_profit,pe,currency,source,confidence,metadata)
SELECT r.document_id,f.forecast_year,f.eps,f.net_profit,f.pe,'CNY','eastmoney:dated_text_v1',.95,f.metadata
FROM eastmoney_reports_dedup s JOIN research.reports r ON r.eastmoney_infocode=s.external_id
CROSS JOIN LATERAL jsonb_to_recordset(coalesce(s.metadata->'dated_forecasts','[]'::jsonb))
 AS f(forecast_year integer,eps numeric,net_profit numeric,pe numeric,metadata jsonb)
WHERE s.report_type='stock'
ON CONFLICT(document_id,forecast_year,source) DO UPDATE SET eps=EXCLUDED.eps,net_profit=EXCLUDED.net_profit,
 pe=EXCLUDED.pe,metadata=EXCLUDED.metadata,confidence=EXCLUDED.confidence
WHERE (report_forecasts.eps,report_forecasts.net_profit,report_forecasts.pe,report_forecasts.metadata)
 IS DISTINCT FROM (EXCLUDED.eps,EXCLUDED.net_profit,EXCLUDED.pe,EXCLUDED.metadata);

INSERT INTO corpus.document_files (
  document_id,
  file_role,
  file_path,
  mime_type,
  size_bytes,
  ocr_status,
  ocr_quality
)
SELECT d.id, file_role, file_path, mime_type, size_bytes, ocr_status, ocr_quality
FROM (
  SELECT source_type, source_name, external_id, 'pdf' AS file_role, NULLIF(pdf_path, '') AS file_path, 'application/pdf' AS mime_type, pdf_size_bytes AS size_bytes, 'none' AS ocr_status, '{}'::jsonb AS ocr_quality
  FROM eastmoney_reports_dedup
  WHERE NULLIF(pdf_path, '') IS NOT NULL
  UNION ALL
  SELECT source_type, source_name, external_id, 'ocr_md', NULLIF(ocr_md_path, ''), 'text/markdown; charset=utf-8', ocr_md_size_bytes, ocr_status, COALESCE(ocr_quality, '{}'::jsonb)
  FROM eastmoney_reports_dedup
  WHERE NULLIF(ocr_md_path, '') IS NOT NULL
  UNION ALL
  SELECT source_type, source_name, external_id, 'ocr_quality_json', NULLIF(ocr_quality_json_path, ''), 'application/json', NULL, ocr_status, COALESCE(ocr_quality, '{}'::jsonb)
  FROM eastmoney_reports_dedup
  WHERE NULLIF(ocr_quality_json_path, '') IS NOT NULL
  UNION ALL
  SELECT source_type, source_name, external_id, 'ocr_quality_md', NULLIF(ocr_quality_md_path, ''), 'text/markdown; charset=utf-8', NULL, ocr_status, COALESCE(ocr_quality, '{}'::jsonb)
  FROM eastmoney_reports_dedup
  WHERE NULLIF(ocr_quality_md_path, '') IS NOT NULL
) f
JOIN corpus.documents d
  ON d.source_type = f.source_type
 AND d.source_name = f.source_name
 AND d.external_id = f.external_id
ON CONFLICT (document_id, file_role, file_path)
DO UPDATE SET
  mime_type = EXCLUDED.mime_type,
  size_bytes = EXCLUDED.size_bytes,
  ocr_status = EXCLUDED.ocr_status,
  ocr_quality = EXCLUDED.ocr_quality;

COMMIT;

-- Keep the multi-table materialization atomic, but do not retain its snapshot
-- while collecting statistics. Long ANALYZE runs would otherwise delay
-- concurrent index validation and keep dead tuples visible unnecessarily.
ANALYZE corpus.documents;
ANALYZE research.reports;
ANALYZE research.report_ratings;
ANALYZE research.report_forecasts;
ANALYZE corpus.document_files;

SELECT
  (SELECT count(*) FROM staging.eastmoney_reports) AS staging_rows,
  (SELECT count(*) FROM eastmoney_reports_dedup) AS dedup_rows,
  (SELECT count(*) FROM research.reports WHERE report_type = 'stock') AS stock_reports,
  (SELECT count(*) FROM research.reports WHERE report_type = 'industry') AS industry_reports,
  (SELECT count(*) FROM corpus.documents WHERE source_type = 'research_report' AND source_name = 'eastmoney') AS document_rows,
  (SELECT count(*) FROM corpus.document_files f JOIN corpus.documents d ON d.id = f.document_id WHERE d.source_type = 'research_report' AND f.file_role = 'pdf') AS pdf_file_rows,
  (SELECT count(*) FROM corpus.document_files f JOIN corpus.documents d ON d.id = f.document_id WHERE d.source_type = 'research_report' AND f.file_role = 'ocr_md') AS ocr_md_file_rows,
  (SELECT count(*) FROM research.report_forecasts) AS forecast_rows;
