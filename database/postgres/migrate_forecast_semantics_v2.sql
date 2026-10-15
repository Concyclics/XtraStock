BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='180s';
SELECT pg_advisory_xact_lock(704201313);
CREATE TABLE IF NOT EXISTS research.forecast_semantics_migrations (
 version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT clock_timestamp(), details jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS research.forecast_semantics_before (
 relation_name text NOT NULL, record_key text NOT NULL, record jsonb NOT NULL,
 saved_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(relation_name,record_key)
);
INSERT INTO research.forecast_semantics_before(relation_name,record_key,record)
 SELECT 'research.report_forecasts',id::text,to_jsonb(f) FROM research.report_forecasts f
 WHERE source='eastmoney' AND coalesce(metadata->>'semantic_contract','')<>'earnings-forecast-v2'
 ON CONFLICT DO NOTHING;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM research.report_forecasts WHERE source='eastmoney' AND net_profit IS NOT NULL AND eps IS NOT NULL AND eps<>net_profit
 AND coalesce(metadata->>'semantic_contract','')<>'earnings-forecast-v2') THEN RAISE EXCEPTION 'EPS/profit conflict; requires review'; END IF;
END $$;
UPDATE research.report_forecasts SET eps=coalesce(eps,net_profit),net_profit=NULL,
 metadata=metadata||jsonb_build_object('semantic_contract','earnings-forecast-v2','eps_unit','CNY/share','pe_unit','times',
 'year_verified',false,'year_basis','legacy_publish_year_bug','excluded_reason','Forecast year was inferred from publication year, not the source response year.',
 'semantics_corrected_at',clock_timestamp())
 WHERE source='eastmoney' AND coalesce(metadata->>'semantic_contract','')<>'earnings-forecast-v2';
COMMENT ON COLUMN research.report_forecasts.eps IS 'Predicted earnings per share, CNY/share; fiscal year must be source verified.';
COMMENT ON COLUMN research.report_forecasts.net_profit IS 'Parent net profit total in CNY only when metadata.net_profit_unit=CNY and scope=parent; never EPS.';
CREATE OR REPLACE VIEW research.report_forecasts_usable AS
 SELECT DISTINCT ON(document_id,forecast_year) f.* FROM research.report_forecasts f
 WHERE metadata->>'semantic_contract'='earnings-forecast-v2' AND metadata->>'year_verified'='true'
 AND (eps IS NULL OR metadata->>'eps_unit'='CNY/share')
 AND (net_profit IS NULL OR (metadata->>'net_profit_unit'='CNY' AND metadata->>'net_profit_scope'='parent'))
 AND (revenue IS NULL OR metadata->>'revenue_unit'='CNY')
 ORDER BY document_id,forecast_year,(source='eastmoney:dated_text_v1') DESC,confidence DESC NULLS LAST,id;
GRANT SELECT ON research.report_forecasts_usable TO finance_app,finance_ai_reader;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='forecast_net_profit' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.forecast_net_profit'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN forecast_net_profit TO invalid_v1_forecast_net_profit');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS forecast_net_profit %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS forecast_eps %s',typ);
 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_forecast_net_profit IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='next_forecast_net_profit' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.next_forecast_net_profit'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN next_forecast_net_profit TO invalid_v1_next_forecast_net_profit');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS next_forecast_net_profit %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS next_forecast_eps %s',typ);
 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_next_forecast_net_profit IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='later_forecast_net_profit' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.later_forecast_net_profit'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN later_forecast_net_profit TO invalid_v1_later_forecast_net_profit');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS later_forecast_net_profit %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS later_forecast_eps %s',typ);
 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_later_forecast_net_profit IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='forecast_net_profit_growth' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.forecast_net_profit_growth'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN forecast_net_profit_growth TO invalid_v1_forecast_net_profit_growth');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS forecast_net_profit_growth %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS forecast_eps_growth %s',typ);
 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_forecast_net_profit_growth IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='consensus_net_profit' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.consensus_net_profit'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN consensus_net_profit TO invalid_v1_consensus_net_profit');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS consensus_net_profit %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS consensus_eps %s',typ);
 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_consensus_net_profit IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='consensus_net_profit_median' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.consensus_net_profit_median'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN consensus_net_profit_median TO invalid_v1_consensus_net_profit_median');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS consensus_net_profit_median %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS consensus_eps_median %s',typ);
 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_consensus_net_profit_median IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='consensus_net_profit_stddev' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.consensus_net_profit_stddev'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN consensus_net_profit_stddev TO invalid_v1_consensus_net_profit_stddev');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS consensus_net_profit_stddev %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS consensus_eps_stddev %s',typ);
 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_consensus_net_profit_stddev IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='forecast_vs_actual_growth' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.forecast_vs_actual_growth'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN forecast_vs_actual_growth TO invalid_v1_forecast_vs_actual_growth');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS forecast_vs_actual_growth %s',typ);

 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_forecast_vs_actual_growth IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='forward_pe' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.forward_pe'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN forward_pe TO invalid_v1_forward_pe');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS forward_pe %s',typ);

 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_forward_pe IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='next_forward_pe' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.next_forward_pe'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN next_forward_pe TO invalid_v1_next_forward_pe');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS next_forward_pe %s',typ);

 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_next_forward_pe IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_forecast_daily'::regclass AND attname='consensus_pe' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_forecast_daily.consensus_pe'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily RENAME COLUMN consensus_pe TO invalid_v1_consensus_pe');
 EXECUTE format('ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS consensus_pe %s',typ);

 COMMENT ON COLUMN analytics.asset_forecast_daily.invalid_v1_consensus_pe IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_snapshot_daily'::regclass AND attname='forecast_net_profit_growth' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_snapshot_daily.forecast_net_profit_growth'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily RENAME COLUMN forecast_net_profit_growth TO invalid_v1_forecast_net_profit_growth');
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS forecast_net_profit_growth %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS forecast_eps_growth %s',typ);
 COMMENT ON COLUMN analytics.asset_snapshot_daily.invalid_v1_forecast_net_profit_growth IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_snapshot_daily'::regclass AND attname='consensus_net_profit' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_snapshot_daily.consensus_net_profit'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily RENAME COLUMN consensus_net_profit TO invalid_v1_consensus_net_profit');
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS consensus_net_profit %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS consensus_eps %s',typ);
 COMMENT ON COLUMN analytics.asset_snapshot_daily.invalid_v1_consensus_net_profit IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_snapshot_daily'::regclass AND attname='consensus_net_profit_median' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_snapshot_daily.consensus_net_profit_median'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily RENAME COLUMN consensus_net_profit_median TO invalid_v1_consensus_net_profit_median');
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS consensus_net_profit_median %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS consensus_eps_median %s',typ);
 COMMENT ON COLUMN analytics.asset_snapshot_daily.invalid_v1_consensus_net_profit_median IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_snapshot_daily'::regclass AND attname='consensus_net_profit_stddev' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_snapshot_daily.consensus_net_profit_stddev'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily RENAME COLUMN consensus_net_profit_stddev TO invalid_v1_consensus_net_profit_stddev');
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS consensus_net_profit_stddev %s',typ);
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS consensus_eps_stddev %s',typ);
 COMMENT ON COLUMN analytics.asset_snapshot_daily.invalid_v1_consensus_net_profit_stddev IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_snapshot_daily'::regclass AND attname='forward_pe' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_snapshot_daily.forward_pe'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily RENAME COLUMN forward_pe TO invalid_v1_forward_pe');
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS forward_pe %s',typ);

 COMMENT ON COLUMN analytics.asset_snapshot_daily.invalid_v1_forward_pe IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.asset_snapshot_daily'::regclass AND attname='next_forward_pe' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.asset_snapshot_daily.next_forward_pe'; END IF;
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily RENAME COLUMN next_forward_pe TO invalid_v1_next_forward_pe');
 EXECUTE format('ALTER TABLE analytics.asset_snapshot_daily ADD COLUMN IF NOT EXISTS next_forward_pe %s',typ);

 COMMENT ON COLUMN analytics.asset_snapshot_daily.invalid_v1_next_forward_pe IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_asset_exposure'::regclass AND attname='forecast_net_profit' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_asset_exposure.forecast_net_profit'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure RENAME COLUMN forecast_net_profit TO invalid_v1_forecast_net_profit');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure ADD COLUMN IF NOT EXISTS forecast_net_profit %s',typ);
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure ADD COLUMN IF NOT EXISTS forecast_eps %s',typ);
 COMMENT ON COLUMN analytics.ai_hardware_asset_exposure.invalid_v1_forecast_net_profit IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_asset_exposure'::regclass AND attname='previous_forecast_net_profit' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_asset_exposure.previous_forecast_net_profit'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure RENAME COLUMN previous_forecast_net_profit TO invalid_v1_previous_forecast_net_profit');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure ADD COLUMN IF NOT EXISTS previous_forecast_net_profit %s',typ);
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure ADD COLUMN IF NOT EXISTS previous_forecast_eps %s',typ);
 COMMENT ON COLUMN analytics.ai_hardware_asset_exposure.invalid_v1_previous_forecast_net_profit IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_asset_exposure'::regclass AND attname='forecast_net_profit_growth' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_asset_exposure.forecast_net_profit_growth'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure RENAME COLUMN forecast_net_profit_growth TO invalid_v1_forecast_net_profit_growth');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure ADD COLUMN IF NOT EXISTS forecast_net_profit_growth %s',typ);
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure ADD COLUMN IF NOT EXISTS forecast_eps_growth %s',typ);
 COMMENT ON COLUMN analytics.ai_hardware_asset_exposure.invalid_v1_forecast_net_profit_growth IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_asset_exposure'::regclass AND attname='forward_pe' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_asset_exposure.forward_pe'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure RENAME COLUMN forward_pe TO invalid_v1_forward_pe');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_asset_exposure ADD COLUMN IF NOT EXISTS forward_pe %s',typ);

 COMMENT ON COLUMN analytics.ai_hardware_asset_exposure.invalid_v1_forward_pe IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_subindustry_daily'::regclass AND attname='avg_forecast_net_profit_growth' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_subindustry_daily.avg_forecast_net_profit_growth'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_subindustry_daily RENAME COLUMN avg_forecast_net_profit_growth TO invalid_v1_avg_forecast_net_profit_growth');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_subindustry_daily ADD COLUMN IF NOT EXISTS avg_forecast_net_profit_growth %s',typ);
 EXECUTE format('ALTER TABLE analytics.ai_hardware_subindustry_daily ADD COLUMN IF NOT EXISTS avg_forecast_eps_growth %s',typ);
 COMMENT ON COLUMN analytics.ai_hardware_subindustry_daily.invalid_v1_avg_forecast_net_profit_growth IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_subindustry_daily'::regclass AND attname='avg_forward_pe' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_subindustry_daily.avg_forward_pe'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_subindustry_daily RENAME COLUMN avg_forward_pe TO invalid_v1_avg_forward_pe');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_subindustry_daily ADD COLUMN IF NOT EXISTS avg_forward_pe %s',typ);

 COMMENT ON COLUMN analytics.ai_hardware_subindustry_daily.invalid_v1_avg_forward_pe IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_theme_scores'::regclass AND attname='forecast_net_profit' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_theme_scores.forecast_net_profit'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores RENAME COLUMN forecast_net_profit TO invalid_v1_forecast_net_profit');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores ADD COLUMN IF NOT EXISTS forecast_net_profit %s',typ);
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores ADD COLUMN IF NOT EXISTS forecast_eps %s',typ);
 COMMENT ON COLUMN analytics.ai_hardware_theme_scores.invalid_v1_forecast_net_profit IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_theme_scores'::regclass AND attname='next_forecast_net_profit' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_theme_scores.next_forecast_net_profit'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores RENAME COLUMN next_forecast_net_profit TO invalid_v1_next_forecast_net_profit');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores ADD COLUMN IF NOT EXISTS next_forecast_net_profit %s',typ);
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores ADD COLUMN IF NOT EXISTS next_forecast_eps %s',typ);
 COMMENT ON COLUMN analytics.ai_hardware_theme_scores.invalid_v1_next_forecast_net_profit IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_theme_scores'::regclass AND attname='forecast_net_profit_growth' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_theme_scores.forecast_net_profit_growth'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores RENAME COLUMN forecast_net_profit_growth TO invalid_v1_forecast_net_profit_growth');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores ADD COLUMN IF NOT EXISTS forecast_net_profit_growth %s',typ);
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores ADD COLUMN IF NOT EXISTS forecast_eps_growth %s',typ);
 COMMENT ON COLUMN analytics.ai_hardware_theme_scores.invalid_v1_forecast_net_profit_growth IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.ai_hardware_theme_scores'::regclass AND attname='forward_pe' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.ai_hardware_theme_scores.forward_pe'; END IF;
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores RENAME COLUMN forward_pe TO invalid_v1_forward_pe');
 EXECUTE format('ALTER TABLE analytics.ai_hardware_theme_scores ADD COLUMN IF NOT EXISTS forward_pe %s',typ);

 COMMENT ON COLUMN analytics.ai_hardware_theme_scores.invalid_v1_forward_pe IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.graph_feature_daily'::regclass AND attname='forecast_net_profit_growth' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.graph_feature_daily.forecast_net_profit_growth'; END IF;
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily RENAME COLUMN forecast_net_profit_growth TO invalid_v1_forecast_net_profit_growth');
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily ADD COLUMN IF NOT EXISTS forecast_net_profit_growth %s',typ);
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily ADD COLUMN IF NOT EXISTS forecast_eps_growth %s',typ);
 COMMENT ON COLUMN analytics.graph_feature_daily.invalid_v1_forecast_net_profit_growth IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.graph_feature_daily'::regclass AND attname='forecast_np_revision_90d' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.graph_feature_daily.forecast_np_revision_90d'; END IF;
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily RENAME COLUMN forecast_np_revision_90d TO invalid_v1_forecast_np_revision_90d');
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily ADD COLUMN IF NOT EXISTS forecast_np_revision_90d %s',typ);
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily ADD COLUMN IF NOT EXISTS forecast_eps_revision_90d %s',typ);
 COMMENT ON COLUMN analytics.graph_feature_daily.invalid_v1_forecast_np_revision_90d IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.graph_feature_daily'::regclass AND attname='forecast_dispersion_180d' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.graph_feature_daily.forecast_dispersion_180d'; END IF;
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily RENAME COLUMN forecast_dispersion_180d TO invalid_v1_forecast_dispersion_180d');
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily ADD COLUMN IF NOT EXISTS forecast_dispersion_180d %s',typ);

 COMMENT ON COLUMN analytics.graph_feature_daily.invalid_v1_forecast_dispersion_180d IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='analytics.graph_feature_daily'::regclass AND attname='forward_earnings_yield' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column analytics.graph_feature_daily.forward_earnings_yield'; END IF;
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily RENAME COLUMN forward_earnings_yield TO invalid_v1_forward_earnings_yield');
 EXECUTE format('ALTER TABLE analytics.graph_feature_daily ADD COLUMN IF NOT EXISTS forward_earnings_yield %s',typ);

 COMMENT ON COLUMN analytics.graph_feature_daily.invalid_v1_forward_earnings_yield IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='factor.feature_daily'::regclass AND attname='graph_forecast_np_growth' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column factor.feature_daily.graph_forecast_np_growth'; END IF;
 EXECUTE format('ALTER TABLE factor.feature_daily RENAME COLUMN graph_forecast_np_growth TO invalid_v1_graph_forecast_np_growth');
 EXECUTE format('ALTER TABLE factor.feature_daily ADD COLUMN IF NOT EXISTS graph_forecast_np_growth %s',typ);
 EXECUTE format('ALTER TABLE factor.feature_daily ADD COLUMN IF NOT EXISTS graph_forecast_eps_growth %s',typ);
 COMMENT ON COLUMN factor.feature_daily.invalid_v1_graph_forecast_np_growth IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='factor.feature_daily'::regclass AND attname='graph_forecast_np_revision_90d' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column factor.feature_daily.graph_forecast_np_revision_90d'; END IF;
 EXECUTE format('ALTER TABLE factor.feature_daily RENAME COLUMN graph_forecast_np_revision_90d TO invalid_v1_graph_forecast_np_revision_90d');
 EXECUTE format('ALTER TABLE factor.feature_daily ADD COLUMN IF NOT EXISTS graph_forecast_np_revision_90d %s',typ);
 EXECUTE format('ALTER TABLE factor.feature_daily ADD COLUMN IF NOT EXISTS graph_forecast_eps_revision_90d %s',typ);
 COMMENT ON COLUMN factor.feature_daily.invalid_v1_graph_forecast_np_revision_90d IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='factor.feature_daily'::regclass AND attname='graph_forecast_dispersion_180d' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column factor.feature_daily.graph_forecast_dispersion_180d'; END IF;
 EXECUTE format('ALTER TABLE factor.feature_daily RENAME COLUMN graph_forecast_dispersion_180d TO invalid_v1_graph_forecast_dispersion_180d');
 EXECUTE format('ALTER TABLE factor.feature_daily ADD COLUMN IF NOT EXISTS graph_forecast_dispersion_180d %s',typ);

 COMMENT ON COLUMN factor.feature_daily.invalid_v1_graph_forecast_dispersion_180d IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
DO $$ DECLARE typ text; BEGIN
 IF NOT EXISTS(SELECT 1 FROM research.forecast_semantics_migrations WHERE version='earnings-forecast-v2') THEN
 SELECT format_type(atttypid,atttypmod) INTO typ FROM pg_attribute WHERE attrelid='factor.feature_daily'::regclass AND attname='graph_forward_earnings_yield' AND NOT attisdropped;
 IF typ IS NULL THEN RAISE EXCEPTION 'Missing column factor.feature_daily.graph_forward_earnings_yield'; END IF;
 EXECUTE format('ALTER TABLE factor.feature_daily RENAME COLUMN graph_forward_earnings_yield TO invalid_v1_graph_forward_earnings_yield');
 EXECUTE format('ALTER TABLE factor.feature_daily ADD COLUMN IF NOT EXISTS graph_forward_earnings_yield %s',typ);

 COMMENT ON COLUMN factor.feature_daily.invalid_v1_graph_forward_earnings_yield IS 'Invalid legacy EPS/year semantics. Preserved solely for audit; excluded from queries and features.';
 END IF;
END $$;
INSERT INTO research.forecast_semantics_before(relation_name,record_key,record)
 SELECT 'company.financial_facts',financial_fact_id::text,to_jsonb(f) FROM company.financial_facts f
 WHERE fact_kind='forecast' AND metadata->>'legacy_table'='research.report_forecasts'
 AND coalesce(metadata->>'semantic_contract','')<>'earnings-forecast-v2'
 ON CONFLICT DO NOTHING;
UPDATE company.financial_facts SET quality_status='rejected',
 metadata=metadata||jsonb_build_object('invalidated_by','earnings-forecast-v2','reason','Legacy EPS/forecast-year semantics; retained for audit only')
 WHERE fact_kind='forecast' AND metadata->>'legacy_table'='research.report_forecasts'
 AND coalesce(metadata->>'semantic_contract','')<>'earnings-forecast-v2'
 AND coalesce(metadata->>'invalidated_by','')<>'earnings-forecast-v2';
INSERT INTO research.forecast_semantics_before(relation_name,record_key,record)
 SELECT 'factor.definitions',factor_id::text,to_jsonb(d) FROM factor.definitions d
 WHERE required_features && ARRAY['graph_forecast_np_growth','graph_forecast_np_revision_90d','graph_forecast_dispersion_180d','graph_forward_earnings_yield']
 ON CONFLICT DO NOTHING;
UPDATE factor.definitions SET lifecycle_status='retired',metadata=metadata||jsonb_build_object(
 'invalidated_by','earnings-forecast-v2','reason','EPS and fiscal-year data contract changed; prior evaluations cannot authorize production'),updated_at=now()
 WHERE required_features && ARRAY['graph_forecast_np_growth','graph_forecast_np_revision_90d','graph_forecast_dispersion_180d','graph_forward_earnings_yield'];
INSERT INTO research.forecast_semantics_migrations(version,details) VALUES('earnings-forecast-v2',
 '{"legacy_values_preserved":true,"strategy_approved":false,"year_bug_requires_revalidation":true}') ON CONFLICT DO NOTHING;
COMMIT;
