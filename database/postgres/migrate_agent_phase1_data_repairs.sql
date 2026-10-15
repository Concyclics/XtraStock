BEGIN;

CREATE INDEX IF NOT EXISTS reports_stock_industry_date_idx
  ON research.reports (stock_code, report_date DESC)
  WHERE industry_name IS NOT NULL AND industry_name <> '';

CREATE OR REPLACE VIEW analytics.asset_industry_current AS
WITH latest_report_industry AS (
  SELECT DISTINCT ON (stock_code)
         stock_code, industry_name, report_date
  FROM research.reports
  WHERE stock_code IS NOT NULL
    AND industry_name IS NOT NULL
    AND industry_name <> ''
  ORDER BY stock_code, report_date DESC, updated_at DESC, document_id DESC
)
SELECT a.asset_id,
       a.symbol AS stock_code,
       a.name AS stock_name,
       a.asset_type,
       a.exchange,
       COALESCE(NULLIF(a.industry_name, ''), r.industry_name) AS industry_name,
       CASE WHEN NULLIF(a.industry_name, '') IS NOT NULL THEN 'market.assets'
            WHEN r.industry_name IS NOT NULL THEN 'latest_research_report'
            ELSE NULL END AS industry_source,
       CASE WHEN NULLIF(a.industry_name, '') IS NOT NULL THEN a.updated_at::date
            ELSE r.report_date END AS industry_source_date
FROM market.assets a
LEFT JOIN latest_report_industry r ON r.stock_code = a.symbol;

COMMENT ON VIEW analytics.asset_industry_current IS
  'Current stock industry classification with market.assets first and latest research report as an evidence-dated fallback.';
GRANT SELECT ON analytics.asset_industry_current TO finance_ai_reader;
GRANT SELECT ON analytics.asset_industry_current TO finance_app;

UPDATE macro.indicators
SET indicator_name = CASE indicator_code
  WHEN 'CPI' THEN '居民消费价格指数同比'
  WHEN 'PPI' THEN '工业生产者出厂价格同比'
  WHEN 'GDP' THEN '国内生产总值同比'
  WHEN 'PMI' THEN '制造业采购经理指数'
  WHEN 'MONEY_SUPPLY' THEN 'M2同比'
  WHEN 'SOCIAL_FINANCE' THEN '社会融资规模'
  WHEN 'LPR' THEN '贷款市场报价利率'
  WHEN 'DEPOSIT_RATE' THEN '基准存款利率'
  WHEN 'FX_RESERVE' THEN '外汇储备'
  WHEN 'INDUSTRY_VALUE_ADD' THEN '规模以上工业增加值同比'
  ELSE indicator_name
END
WHERE indicator_name IS NULL;

UPDATE macro.indicator_values v
SET value = CASE i.indicator_code
  WHEN 'CPI' THEN NULLIF(v.raw_payload->>'NATIONAL_SAME', '')::numeric
  WHEN 'PPI' THEN NULLIF(v.raw_payload->>'BASE_SAME', '')::numeric
  WHEN 'GDP' THEN NULLIF(v.raw_payload->>'SUM_SAME', '')::numeric
  WHEN 'PMI' THEN NULLIF(v.raw_payload->>'MAKE_INDEX', '')::numeric
  WHEN 'MONEY_SUPPLY' THEN NULLIF(v.raw_payload->>'BASIC_CURRENCY_SAME', '')::numeric
  WHEN 'SOCIAL_FINANCE' THEN NULLIF(v.raw_payload->>'TOTAL_SAME', '')::numeric
  WHEN 'LPR' THEN NULLIF(v.raw_payload->>'LPR1Y', '')::numeric
  WHEN 'DEPOSIT_RATE' THEN NULLIF(v.raw_payload->>'DEPOSIT_RATE_BA', '')::numeric
  WHEN 'FX_RESERVE' THEN NULLIF(v.raw_payload->>'GOLD_RESERVES', '')::numeric
  WHEN 'INDUSTRY_VALUE_ADD' THEN NULLIF(v.raw_payload->>'BASE_SAME', '')::numeric
  ELSE v.value
END
FROM macro.indicators i
WHERE i.indicator_id = v.indicator_id
  AND v.value IS NULL;

COMMIT;
