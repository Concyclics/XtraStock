BEGIN;
SET LOCAL lock_timeout='5s';
ALTER TABLE research.report_forecasts ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
CREATE TABLE IF NOT EXISTS research.report_forecast_revision_history (
 revision_id bigserial PRIMARY KEY,forecast_id bigint NOT NULL,record jsonb NOT NULL,
 superseded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE OR REPLACE FUNCTION research.preserve_forecast_revision() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,research AS $$
BEGIN
 IF (to_jsonb(NEW)-ARRAY['updated_at']) IS NOT DISTINCT FROM (to_jsonb(OLD)-ARRAY['updated_at']) THEN RETURN NULL; END IF;
 INSERT INTO research.report_forecast_revision_history(forecast_id,record) VALUES(OLD.id,to_jsonb(OLD));
 NEW.updated_at:=clock_timestamp(); RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS preserve_forecast_revision ON research.report_forecasts;
CREATE TRIGGER preserve_forecast_revision BEFORE UPDATE ON research.report_forecasts
 FOR EACH ROW EXECUTE FUNCTION research.preserve_forecast_revision();
CREATE OR REPLACE FUNCTION research.reject_forecast_history_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'Forecast revision history is append-only';
END $$;
DROP TRIGGER IF EXISTS reject_forecast_history_mutation ON research.report_forecast_revision_history;
CREATE TRIGGER reject_forecast_history_mutation BEFORE UPDATE OR DELETE OR TRUNCATE
 ON research.report_forecast_revision_history FOR EACH STATEMENT
 EXECUTE FUNCTION research.reject_forecast_history_mutation();
CREATE OR REPLACE VIEW research.report_forecasts_usable AS
 SELECT DISTINCT ON(document_id,forecast_year) f.* FROM research.report_forecasts f
 WHERE metadata->>'semantic_contract'='earnings-forecast-v2' AND metadata->>'year_verified'='true'
 AND (eps IS NULL OR metadata->>'eps_unit'='CNY/share')
 AND (net_profit IS NULL OR (metadata->>'net_profit_unit'='CNY' AND metadata->>'net_profit_scope'='parent'))
 AND (revenue IS NULL OR metadata->>'revenue_unit'='CNY')
 ORDER BY document_id,forecast_year,(source='eastmoney:dated_text_v1') DESC,confidence DESC NULLS LAST,id;
GRANT SELECT ON research.report_forecasts_usable,research.report_forecast_revision_history TO finance_app,finance_ai_reader;
COMMIT;
