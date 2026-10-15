BEGIN;

GRANT USAGE ON SCHEMA macro TO finance_ai_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA macro TO finance_ai_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA macro GRANT SELECT ON TABLES TO finance_ai_reader;

CREATE INDEX IF NOT EXISTS factors_daily_asset_factor_ts_idx
  ON market.factors_daily (asset_id, factor_name, ts DESC);
CREATE INDEX IF NOT EXISTS events_raw_asset_category_date_idx
  ON market.events_raw (asset_id, category, event_date DESC);
CREATE INDEX IF NOT EXISTS assets_stock_industry_symbol_idx
  ON market.assets (industry_name, symbol)
  WHERE asset_type = 'stock';

INSERT INTO agent.metric_catalog (
  metric_key, display_name, description, canonical_relation, value_columns,
  grain, time_column, available_at_column, dimension_columns, unit, formula,
  freshness_slo, null_policy, lookahead_risk, status, metadata
)
VALUES
  (
    'source_financial_metrics', '公司源报表指标',
    '东方财富公司财务报表中的营收、归母净利润、EPS、ROE及同比指标。',
    'company.financial_metrics', ARRAY['metric_value'], '一只资产一个报告期一个指标',
    'report_date', 'created_at', ARRAY['asset_id','metric_name','source'], 'source_specific', NULL,
    INTERVAL '120 days', '缺失表示源数据未提供，不得按0处理', 'point_in_time_required', 'active',
    '{"tool":"finance_fundamental_analysis","percentage_fields":"percentage_points"}'::jsonb
  ),
  (
    'historical_valuation', '历史估值',
    '按交易日记录的PE-TTM和PB，固定点原始值除以100后输出。',
    'market.factors_daily', ARRAY['factor_value'], '一只资产一个交易日一个估值指标',
    'ts', 'created_at', ARRAY['asset_id','factor_name','source'], 'times', 'factor_value / 100',
    INTERVAL '2 days', '非正PE表示亏损，不进入正PE历史分位', 'point_in_time_required', 'active',
    '{"tool":"finance_relative_valuation"}'::jsonb
  ),
  (
    'capital_flow', '资金流',
    '按来源订单规模分类的主力、小单、中单、大单和超大单净流入。',
    'market.money_flow_daily', ARRAY['main_net','small_net','mid_net','big_net','super_net'],
    '一只资产一个交易日一个来源', 'ts', 'created_at', ARRAY['asset_id','source'], 'CNY', NULL,
    INTERVAL '2 days', '缺失不代表净流入为0', 'point_in_time_required', 'active',
    '{"tool":"finance_capital_flow_analysis","warning":"source-defined buckets do not prove investor identity"}'::jsonb
  ),
  (
    'macro_observation', '宏观指标观测值',
    'CPI、PPI、GDP、PMI、M2、社融、LPR及汇率等宏观源观测值。',
    'macro.indicator_values', ARRAY['value'], '一个指标一个观测期一个来源',
    'observed_at', 'created_at', ARRAY['indicator_id','period','source'], 'source_specific', NULL,
    INTERVAL '45 days', '不同指标频率和单位不同', 'point_in_time_required', 'active',
    '{"tool":"finance_macro_context","warning":"publication lag and revisions apply"}'::jsonb
  )
ON CONFLICT (metric_key) DO UPDATE SET
  display_name=EXCLUDED.display_name, description=EXCLUDED.description,
  canonical_relation=EXCLUDED.canonical_relation, value_columns=EXCLUDED.value_columns,
  grain=EXCLUDED.grain, time_column=EXCLUDED.time_column,
  available_at_column=EXCLUDED.available_at_column, dimension_columns=EXCLUDED.dimension_columns,
  unit=EXCLUDED.unit, formula=EXCLUDED.formula, freshness_slo=EXCLUDED.freshness_slo,
  null_policy=EXCLUDED.null_policy, lookahead_risk=EXCLUDED.lookahead_risk,
  status=EXCLUDED.status, metadata=EXCLUDED.metadata, updated_at=now();

INSERT INTO agent.query_catalog (
  query_key, intent, tool_name, description, source_relations, input_schema,
  output_schema, freshness_requirements, evidence_requirements, safety_notes, status
)
VALUES
  ('fundamental_analysis', '公司实际基本面趋势', 'finance_fundamental_analysis',
   '查询公司源报表的营收、利润、EPS、ROE和同比趋势。',
   ARRAY['company.financial_metrics','market.assets'],
   '{"required_any":["stock_code","stock_name"]}'::jsonb,
   '{"requires":["report_date","units","dataset_latest_report_date","coverage_pct"]}'::jsonb,
   '报告期数据通常按季度更新', '必须保留报告期、来源和单位', '不得与研报预测混用', 'active'),
  ('peer_compare', '同行比较', 'finance_peer_compare',
   '在同一快照日比较同行估值、增长、收益和研报覆盖。',
   ARRAY['analytics.asset_snapshot_daily','market.assets'],
   '{"required_any":["stock_code","stock_name","industry","peer_stock_codes"]}'::jsonb,
   '{"requires":["as_of_date","peer_universe_count","source_dates"]}'::jsonb,
   '使用同一as_of_date', '必须披露同行范围和缺失指标', '当前行业分类可能较宽', 'active'),
  ('relative_valuation', '历史相对估值', 'finance_relative_valuation',
   '计算股票自身历史PE/PB区间和分位。', ARRAY['market.factors_daily','market.assets'],
   '{"required_any":["stock_code","stock_name"]}'::jsonb,
   '{"requires":["as_of_date","lookback_days","observation_count"]}'::jsonb,
   '估值交易日水位不超过2天', '必须披露样本数量和截止日期', '低分位不等于买入建议', 'active'),
  ('capital_flow_analysis', '资金流与市场事件', 'finance_capital_flow_analysis',
   '查询单股资金流、龙虎榜、大宗、两融、解禁、调研及北向数据。',
   ARRAY['market.money_flow_daily','market.events_raw','market.assets'],
   '{"required_any":["stock_code","stock_name"]}'::jsonb,
   '{"requires":["dataset_coverage","missing_categories","units"]}'::jsonb,
   '各事件类别水位独立披露', '必须保留来源类别和日期', '资金流分类不能证明机构意图', 'active'),
  ('macro_context', '宏观环境', 'finance_macro_context',
   '查询本地宏观指标历史。', ARRAY['macro.indicators','macro.indicator_values'],
   '{"optional":["indicators","start_date","end_date","periods"]}'::jsonb,
   '{"requires":["observed_at","source_unit","dataset_latest_observation"]}'::jsonb,
   '按指标频率判断', '必须保留观测期、来源和单位', '历史分析需考虑发布时间与修订', 'active')
ON CONFLICT (query_key) DO UPDATE SET
  intent=EXCLUDED.intent, tool_name=EXCLUDED.tool_name, description=EXCLUDED.description,
  source_relations=EXCLUDED.source_relations, input_schema=EXCLUDED.input_schema,
  output_schema=EXCLUDED.output_schema, freshness_requirements=EXCLUDED.freshness_requirements,
  evidence_requirements=EXCLUDED.evidence_requirements, safety_notes=EXCLUDED.safety_notes,
  status=EXCLUDED.status, updated_at=now();

INSERT INTO agent.data_contracts (
  dataset_key, relation_name, grain, primary_key_columns, watermark_column,
  freshness_slo, required_columns, quality_checks
)
VALUES
  ('source_financials', 'company.financial_metrics', '一只资产一个报告期一个指标一个来源',
   ARRAY['asset_id','report_date','metric_name','source'], 'created_at', INTERVAL '120 days',
   ARRAY['asset_id','report_date','metric_name','metric_value','source'],
   '["百分比指标按百分点解释","同一趋势必须比较可比报告期"]'::jsonb),
  ('capital_flow', 'market.money_flow_daily', '一只资产一个交易日一个来源',
   ARRAY['asset_id','ts','source'], 'ts', INTERVAL '2 days',
   ARRAY['asset_id','ts','source'], '["水位按交易日检查","缺失不得填0"]'::jsonb),
  ('market_events', 'market.events_raw', '一个来源事件一行', ARRAY['id'], 'event_date', INTERVAL '7 days',
   ARRAY['category','event_date','source_hash','raw_payload'],
   '["各category独立检查水位","JSON字段解析必须保留原始行"]'::jsonb),
  ('macro_values', 'macro.indicator_values', '一个指标一个观测期一个来源',
   ARRAY['indicator_id','period','source'], 'observed_at', INTERVAL '45 days',
   ARRAY['indicator_id','period','value','source'],
   '["单位必须随结果披露","历史使用需检查可得时间"]'::jsonb)
ON CONFLICT (dataset_key) DO UPDATE SET
  relation_name=EXCLUDED.relation_name, grain=EXCLUDED.grain,
  primary_key_columns=EXCLUDED.primary_key_columns, watermark_column=EXCLUDED.watermark_column,
  freshness_slo=EXCLUDED.freshness_slo, required_columns=EXCLUDED.required_columns,
  quality_checks=EXCLUDED.quality_checks, updated_at=now();

COMMIT;
