BEGIN;
-- Keep canonical-code predicates indexable. COALESCE over a LEFT JOIN forces
-- repeated full report scans in the graph's next-report and Agent queries.
CREATE OR REPLACE VIEW research.reports_resolved AS
SELECT r.document_id,r.report_type,r.eastmoney_infocode,r.report_date,r.report_title,
       r.institution,r.analyst_names,r.rating,r.rating_change,r.industry_name,
       r.stock_code,r.stock_name,r.source_url,
       r.web_text_status,r.pdf_status,r.ocr_status,r.metadata,r.created_at,r.updated_at,r.available_at,
       r.stock_code AS source_stock_code,NULL::timestamptz AS identity_known_at,
       NULL::date AS code_switch_date
FROM research.reports r
WHERE NOT EXISTS (SELECT 1 FROM market.research_security_code_aliases a WHERE a.old_code=r.stock_code)
UNION ALL
SELECT r.document_id,r.report_type,r.eastmoney_infocode,r.report_date,r.report_title,
       r.institution,r.analyst_names,r.rating,r.rating_change,r.industry_name,
       a.new_code,r.stock_name,r.source_url,
       r.web_text_status,r.pdf_status,r.ocr_status,r.metadata,r.created_at,r.updated_at,r.available_at,
       r.stock_code AS source_stock_code,a.recorded_at AS identity_known_at,
       a.switch_date AS code_switch_date
FROM market.research_security_code_aliases a
JOIN research.reports r ON r.stock_code=a.old_code;
COMMENT ON VIEW research.reports_resolved IS
 'Current research identity resolution. Raw code remains source_stock_code. For system PIT include identity_known_at in knowledge cutoff. Does not assert historical tradability. Disjoint branches retain indexed code lookups.';
GRANT SELECT ON research.reports_resolved TO finance_app,finance_ai_reader;
COMMIT;
