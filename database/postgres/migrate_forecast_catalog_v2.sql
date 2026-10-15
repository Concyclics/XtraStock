BEGIN;
SET LOCAL lock_timeout='5s';
UPDATE factor.definitions SET version=version+1,metadata=metadata||'{"score_invalidation":"earnings-forecast-v2"}'::jsonb,updated_at=now()
WHERE metadata->>'invalidated_by'='earnings-forecast-v2' AND coalesce(metadata->>'score_invalidation','')<>'earnings-forecast-v2';
UPDATE agent.metric_catalog SET value_columns=ARRAY['forecast_eps','consensus_eps','forecast_net_profit','consensus_net_profit','forward_pe','latest_rating'],
 description='按机构和预测年度去重的研报预期。每股收益与归母净利润总额分列，不能混为实际财报。',
 unit='EPS: CNY/share；归母净利润总额: CNY；PE: 倍',formula='每机构每预测年度取最新一份研报，EPS 与归母净利润分别聚合',
 metadata=metadata||'{"semantic_contract":"earnings-forecast-v2"}'::jsonb,updated_at=now() WHERE metric_key='research_forecast_consensus';
UPDATE agent.metric_catalog SET description='明确预测年度的原研报 PE，价格时点是研报日期；不等于最新价格除以 EPS。',
 formula='原研报明确年度 PE；价格日期见 latest_report_date',
 metadata=metadata||'{"semantic_contract":"earnings-forecast-v2","source_relation":"research.report_forecasts_usable","price_basis":"source_report_date"}'::jsonb,
 updated_at=now() WHERE metric_key='forward_pe';
UPDATE agent.query_catalog SET output_schema=replace(replace(output_schema::text,'forecast_net_profit_growth','forecast_eps_growth'),
 'consensus_net_profit','consensus_eps')::jsonb,updated_at=now()
WHERE output_schema::text LIKE '%forecast_net_profit_growth%' OR output_schema::text LIKE '%consensus_net_profit%';
COMMIT;
