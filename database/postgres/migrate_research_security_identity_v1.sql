BEGIN;
-- Document labels remain immutable in research.reports. This table resolves
-- research identity, not historical trading eligibility or NEEQ/BSE price joins.
CREATE TABLE IF NOT EXISTS market.research_security_code_aliases (
  old_code text PRIMARY KEY CHECK (old_code ~ '^[0-9]{6}$'),
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id),
  new_code text NOT NULL CHECK (new_code ~ '^[0-9]{6}$'),
  exchange text NOT NULL,
  security_name text NOT NULL,
  switch_date date NOT NULL,
  exchange_table_listing_date date NOT NULL,
  source_url text NOT NULL,
  source_sha256 text NOT NULL,
  evidence jsonb NOT NULL,
  observed_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (old_code <> new_code)
);
GRANT SELECT ON market.research_security_code_aliases TO finance_app, finance_ai_reader;
CREATE OR REPLACE VIEW research.reports_resolved AS
SELECT r.document_id,r.report_type,r.eastmoney_infocode,r.report_date,r.report_title,
       r.institution,r.analyst_names,r.rating,r.rating_change,r.industry_name,
       coalesce(a.new_code,r.stock_code) AS stock_code,r.stock_name,r.source_url,
       r.web_text_status,r.pdf_status,r.ocr_status,r.metadata,r.created_at,r.updated_at,r.available_at,
       r.stock_code AS source_stock_code,a.recorded_at AS identity_known_at,
       a.switch_date AS code_switch_date
FROM research.reports r
LEFT JOIN market.research_security_code_aliases a ON a.old_code=r.stock_code;
COMMENT ON VIEW research.reports_resolved IS
 'Current research identity resolution. Raw code remains source_stock_code. For system PIT include identity_known_at in knowledge cutoff. Does not assert historical tradability.';
GRANT SELECT ON research.reports_resolved TO finance_app, finance_ai_reader;
COMMIT;
