BEGIN;
SET LOCAL lock_timeout='5s';
ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS forecast_report_date date;
ALTER TABLE analytics.asset_forecast_daily ADD COLUMN IF NOT EXISTS forecast_document_id uuid;
COMMENT ON COLUMN analytics.asset_forecast_daily.forecast_report_date IS
 'Date of the report supplying forecast_eps/net_profit/PE. May differ from latest_report_date, which supplies rating.';
UPDATE agent.metric_catalog SET formula='原研报明确年度 PE；报告日期见 forecast_report_date，引用价格可能来自前一收盘',
 metadata=metadata||jsonb_build_object('price_basis','source_report_reference_price','forecast_date_key','forecast_report_date'),updated_at=now()
 WHERE metric_key='forward_pe';
UPDATE agent.metric_catalog SET display_name='研报预测归母净利润总额增速',
 description='同一研报相邻预测年度的归母净利润总额增长；原始总额统一人民币元，EPS 不参与此计算。',
 formula='next_forecast_net_profit / forecast_net_profit - 1，分母须为正且来自同一研报',
 metadata=metadata||jsonb_build_object('semantic_contract','earnings-forecast-v2','source_relation','research.report_forecasts_usable'),updated_at=now()
 WHERE metric_key='forecast_net_profit_growth';
COMMIT;
