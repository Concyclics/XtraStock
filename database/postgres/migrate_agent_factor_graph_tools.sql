BEGIN;

INSERT INTO agent.metric_catalog (
  metric_key, display_name, description, canonical_relation, value_columns,
  grain, time_column, available_at_column, dimension_columns, unit, formula,
  freshness_slo, null_policy, lookahead_risk, status, metadata
)
VALUES
  (
    'factor_daily_score', '因子日度横截面分数',
    '方向调整后的因子原值、当日横截面分位和Z分数。高分位表示按因子定义方向更强，不等于上涨概率。',
    'factor.daily_scores', ARRAY['raw_value','percentile','zscore'],
    '一个因子/一只资产/一个交易日', 'dt', 'available_at',
    ARRAY['factor_id','asset_id'], 'score',
    'raw_value 已乘 factor.definitions.direction；percentile 和 zscore 在同一交易日横截面计算',
    INTERVAL '2 days', '缺失表示当日特征不可计算，不得填零', 'point_in_time_required', 'active',
    '{"lifecycle_rule":"external agents default to shadow/production; candidate requires explicit opt-in","not_probability":true}'::jsonb
  ),
  (
    'factor_feature_daily', '因子日度特征矩阵',
    '用于研究和因子计算的点时特征宽表，包含价格、流动性、研报、事件和图谱特征。不同列原值不可直接横向比较。',
    'factor.feature_daily',
    ARRAY['momentum_20d','low_volatility_20d','research_breadth_20d','graph_event_impulse_30d','graph_centrality'],
    '一只资产/一个交易日', 'dt', 'available_at', ARRAY['asset_id'], 'mixed',
    '由 point-in-time 来源按 dt 汇总；完整来源日期保存在 source_dates',
    INTERVAL '2 days', '来源不足时相关特征为NULL', 'point_in_time_required', 'active',
    '{"source_dates_column":"source_dates","raw_features_not_cross_comparable":true}'::jsonb
  ),
  (
    'factor_validation', '因子样本外评估',
    'Walk-forward样本外Rank IC、分组收益差、稳定性、换手率、p值和多重检验q值。通过单次门槛不等于生产级可交易。',
    'factor.evaluations',
    ARRAY['rank_ic_mean','rank_ic_ir','sign_consistency','top_bottom_spread_mean','turnover_mean','p_value','q_value','passed_gates'],
    '一次实验/一个因子/一个样本分组', 'created_at', 'created_at',
    ARRAY['run_id','factor_id','sample_name'], 'mixed', NULL,
    INTERVAL '8 days', '无评估记录表示尚未验证，不得解释为无效或有效', 'none', 'active',
    '{"join_run":"factor.experiment_runs","production_requires_lifecycle":"production"}'::jsonb
  ),
  (
    'temporal_graph_path', '时序知识图谱路径',
    '在有效时间和知识时间双重约束下连接公司、行业、概念、产品、材料、设备、事件和机构。每条边必须保留来源和证据。',
    'kg.relation_versions', ARRAY['confidence','weight','direction'],
    '一条关系版本/一个有效时间区间/一个知识时间区间', 'valid_during', 'system_known_during',
    ARRAY['relation_id','source_entity_id','target_entity_id','relation_type'], 'score', NULL,
    INTERVAL '2 days', '缺边可能是覆盖不足，不得证明关系不存在', 'point_in_time_required', 'active',
    '{"coverage_relation":"kg.source_coverage_daily","causality_warning":true,"max_agent_depth":3}'::jsonb
  )
ON CONFLICT (metric_key) DO UPDATE SET
  display_name=EXCLUDED.display_name, description=EXCLUDED.description,
  canonical_relation=EXCLUDED.canonical_relation, value_columns=EXCLUDED.value_columns,
  grain=EXCLUDED.grain, time_column=EXCLUDED.time_column,
  available_at_column=EXCLUDED.available_at_column,
  dimension_columns=EXCLUDED.dimension_columns, unit=EXCLUDED.unit,
  formula=EXCLUDED.formula, freshness_slo=EXCLUDED.freshness_slo,
  null_policy=EXCLUDED.null_policy, lookahead_risk=EXCLUDED.lookahead_risk,
  status=EXCLUDED.status, metadata=EXCLUDED.metadata, updated_at=now();

INSERT INTO agent.query_catalog (
  query_key, intent, tool_name, description, source_relations, input_schema,
  output_schema, freshness_requirements, evidence_requirements, safety_notes, status
)
VALUES
  ('factor_catalog', '查询因子定义、生命周期和最近验证结果', 'finance_factor_catalog',
   '列出可用因子及最新样本外评估，不执行选股。',
   ARRAY['factor.definitions','factor.evaluations','factor.experiment_runs','factor.daily_scores'],
   '{"optional":["factor_name","family","lifecycle_status"]}'::jsonb,
   '{"requires":["lifecycle_status","direction","score_start_date","score_end_date","latest_validation"]}'::jsonb,
   '评分水位不超过2个交易日', '验证结果必须带测试区间、样本数和q值',
   'candidate仅用于研究发现；passed_gates不等于production', 'active'),
  ('factor_profile', '解释单只股票的因子状态和历史变化', 'finance_factor_profile',
   '返回指定股票的方向调整分位、Z分数、可得时间和近期变化。',
   ARRAY['factor.daily_scores','factor.definitions','market.assets'],
   '{"required_any":["stock_code","stock_name"],"optional":["as_of_date","factor_names","lookback_days"]}'::jsonb,
   '{"requires":["dt","available_at","source_feature_date","lifecycle_status","percentile"]}'::jsonb,
   '评分水位不超过2个交易日', '数值来自物化因子分数，不是文档事实',
   '分位不是上涨概率；不同日期样本池可能变化', 'active'),
  ('factor_validation', '检查因子样本外有效性与稳定性', 'finance_factor_validation',
   '查询实验区间、Rank IC、分组收益、换手率、p/q值和门槛状态。',
   ARRAY['factor.definitions','factor.evaluations','factor.experiment_runs'],
   '{"required_any":["factor_name","family"],"optional":["sample_name","passed_only"]}'::jsonb,
   '{"requires":["train_start","test_start","test_end","observation_count","q_value","passed_gates"]}'::jsonb,
   '每周研究任务更新', '必须披露样本外测试区间和多重检验结果',
   '不得把单次通过解释为保证收益', 'active'),
  ('factor_market_screen', '在全市场结构化筛选中加入受控因子条件', 'finance_market_screen',
   '在估值、业绩、研报和收益筛选基础上，按同日因子分位/Z分数过滤。',
   ARRAY['analytics.asset_snapshot_daily','factor.daily_scores','factor.definitions'],
   '{"optional":["factor_filters","factor_match","factor_lifecycle"]}'::jsonb,
   '{"requires":["snapshot_date","factor_score_date","factor_values","coverage_pct"]}'::jsonb,
   '统一使用不晚于请求日的共同日期', '返回每个候选的实际因子值和生命周期',
   '默认禁止candidate；原始值不可跨因子比较', 'active'),
  ('temporal_graph_paths', '查询最多三跳且逐边带证据的时序产业链路径', 'finance_graph_paths',
   '从公司、行业或概念出发，在有效时间和公开/系统知识时间约束下发现路径。',
   ARRAY['kg.market_entities','kg.relations','kg.relation_versions','kg.relation_version_evidence','kg.source_coverage_daily'],
   '{"required_any":["query","stock_code"],"optional":["target_query","max_depth","relation_types","as_of_date","known_at"]}'::jsonb,
   '{"requires":["path_entities","path_edges","edge_evidence","coverage_score"]}'::jsonb,
   '图谱水位不超过2天', '每条边必须返回关系版本、来源和最佳证据',
   '路径表示证据关联而非因果；缺路径不证明无关系', 'active')
ON CONFLICT (query_key) DO UPDATE SET
  intent=EXCLUDED.intent, tool_name=EXCLUDED.tool_name,
  description=EXCLUDED.description, source_relations=EXCLUDED.source_relations,
  input_schema=EXCLUDED.input_schema, output_schema=EXCLUDED.output_schema,
  freshness_requirements=EXCLUDED.freshness_requirements,
  evidence_requirements=EXCLUDED.evidence_requirements,
  safety_notes=EXCLUDED.safety_notes, status=EXCLUDED.status, updated_at=now();

INSERT INTO agent.data_contracts (
  dataset_key, relation_name, grain, primary_key_columns, watermark_column,
  freshness_slo, required_columns, quality_checks, status
)
VALUES
  ('factor_features', 'factor.feature_daily', '一只资产一个交易日',
   ARRAY['asset_id','dt'], 'available_at', INTERVAL '2 days',
   ARRAY['asset_id','dt','available_at','source_dates'],
   '["available_at不得晚于决策截止时间","source_dates必须保留","NULL不得填零"]'::jsonb, 'active'),
  ('factor_scores', 'factor.daily_scores', '一个因子一只资产一个交易日',
   ARRAY['factor_id','asset_id','dt'], 'available_at', INTERVAL '2 days',
   ARRAY['factor_id','asset_id','dt','percentile','zscore','available_at','source_feature_date'],
   '["factor_id必须存在于definitions","source_feature_date不得晚于dt","外部筛选默认排除candidate"]'::jsonb, 'active'),
  ('factor_evaluations', 'factor.evaluations', '一次实验一个因子一个样本分组',
   ARRAY['run_id','factor_id','sample_name'], 'created_at', INTERVAL '8 days',
   ARRAY['run_id','factor_id','observation_count','date_count','q_value','passed_gates'],
   '["必须连接experiment_runs披露测试区间","生产结论必须检查lifecycle_status"]'::jsonb, 'active'),
  ('temporal_graph_paths', 'kg.relation_versions', '一条关系的一个双时态版本',
   ARRAY['relation_version_id'], 'system_known_during', INTERVAL '2 days',
   ARRAY['relation_id','valid_during','public_known_during','system_known_during','confidence','status'],
   '["必须按as_of和known_at过滤","拒绝和归档版本不可返回","每条事实声明需要证据"]'::jsonb, 'active')
ON CONFLICT (dataset_key) DO UPDATE SET
  relation_name=EXCLUDED.relation_name, grain=EXCLUDED.grain,
  primary_key_columns=EXCLUDED.primary_key_columns,
  watermark_column=EXCLUDED.watermark_column, freshness_slo=EXCLUDED.freshness_slo,
  required_columns=EXCLUDED.required_columns, quality_checks=EXCLUDED.quality_checks,
  status=EXCLUDED.status, updated_at=now();

COMMIT;
