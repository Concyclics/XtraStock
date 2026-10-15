BEGIN;

CREATE SCHEMA IF NOT EXISTS agent;

COMMENT ON SCHEMA agent IS
  'Agent-facing semantic catalog: metric definitions, approved query intents, and dataset quality contracts.';

CREATE TABLE IF NOT EXISTS agent.metric_catalog (
  metric_key text PRIMARY KEY,
  display_name text NOT NULL,
  description text NOT NULL,
  canonical_relation text NOT NULL,
  value_columns text[] NOT NULL,
  grain text NOT NULL,
  time_column text,
  available_at_column text,
  dimension_columns text[] NOT NULL DEFAULT '{}'::text[],
  unit text,
  formula text,
  freshness_slo interval,
  null_policy text,
  lookahead_risk text NOT NULL DEFAULT 'none',
  status text NOT NULL DEFAULT 'active',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT metric_catalog_status_chk CHECK (status IN ('active', 'experimental', 'deprecated')),
  CONSTRAINT metric_catalog_lookahead_chk CHECK (lookahead_risk IN ('none', 'point_in_time_required', 'unknown'))
);

CREATE TABLE IF NOT EXISTS agent.query_catalog (
  query_key text PRIMARY KEY,
  intent text NOT NULL,
  tool_name text NOT NULL,
  description text NOT NULL,
  source_relations text[] NOT NULL,
  input_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
  freshness_requirements text,
  evidence_requirements text,
  safety_notes text,
  status text NOT NULL DEFAULT 'active',
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT query_catalog_status_chk CHECK (status IN ('active', 'experimental', 'deprecated'))
);

CREATE TABLE IF NOT EXISTS agent.data_contracts (
  dataset_key text PRIMARY KEY,
  relation_name text NOT NULL,
  grain text NOT NULL,
  primary_key_columns text[] NOT NULL,
  watermark_column text,
  freshness_slo interval,
  required_columns text[] NOT NULL DEFAULT '{}'::text[],
  quality_checks jsonb NOT NULL DEFAULT '[]'::jsonb,
  owner text NOT NULL DEFAULT 'finance_pipeline',
  status text NOT NULL DEFAULT 'active',
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT data_contracts_status_chk CHECK (status IN ('active', 'experimental', 'deprecated'))
);

CREATE TABLE IF NOT EXISTS analytics.asset_snapshot_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  stock_code text NOT NULL,
  stock_name text,
  as_of_date date NOT NULL,
  technical_date date,
  research_date date,
  forecast_date date,
  signal_date date,
  close double precision,
  pct_chg_1d double precision,
  ret_5d double precision,
  ret_20d double precision,
  volatility_20d double precision,
  drawdown_20d double precision,
  pattern_tags text[] NOT NULL DEFAULT '{}'::text[],
  pe_ttm numeric,
  pb numeric,
  forward_pe numeric,
  next_forward_pe numeric,
  forecast_net_profit_growth double precision,
  forecast_eps_growth double precision,
  consensus_net_profit numeric,
  consensus_eps numeric,
  consensus_net_profit_median numeric,
  consensus_eps_median numeric,
  consensus_net_profit_stddev numeric,
  consensus_eps_stddev numeric,
  forecast_institution_count_180d integer,
  latest_rating text,
  latest_rating_change text,
  recent_report_count integer,
  positive_rating_count integer,
  upgrade_count integer,
  composite_score double precision,
  actual_report_date date,
  actual_report_period text,
  actual_revenue_yoy double precision,
  actual_net_profit_yoy double precision,
  actual_deducted_net_profit_yoy double precision,
  actual_financial_confidence numeric,
  source_dates jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, as_of_date)
);

ALTER TABLE analytics.asset_snapshot_daily
  ADD COLUMN IF NOT EXISTS actual_report_date date,
  ADD COLUMN IF NOT EXISTS actual_report_period text,
  ADD COLUMN IF NOT EXISTS actual_revenue_yoy double precision,
  ADD COLUMN IF NOT EXISTS actual_net_profit_yoy double precision,
  ADD COLUMN IF NOT EXISTS actual_deducted_net_profit_yoy double precision,
  ADD COLUMN IF NOT EXISTS actual_financial_confidence numeric;

COMMENT ON TABLE analytics.asset_snapshot_daily IS
  'Point-in-time agent-facing stock snapshot combining technical, valuation, forecast, research and fused signal data.';
COMMENT ON COLUMN analytics.asset_snapshot_daily.as_of_date IS
  'Snapshot date. Use this date, not current rows, for historical screens and backtests.';
COMMENT ON COLUMN analytics.asset_snapshot_daily.source_dates IS
  'Actual source dates used for each component; agents must disclose them when the components are stale.';
COMMENT ON COLUMN analytics.asset_snapshot_daily.pe_ttm IS
  'TTM PE normalized from market.factors_daily fixed-point storage (raw value / 100).';
COMMENT ON COLUMN analytics.asset_snapshot_daily.pb IS
  'PB normalized from market.factors_daily fixed-point storage (raw value / 100).';

CREATE INDEX IF NOT EXISTS asset_snapshot_daily_date_score_idx
  ON analytics.asset_snapshot_daily (as_of_date DESC, composite_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS asset_snapshot_daily_stock_date_idx
  ON analytics.asset_snapshot_daily (stock_code, as_of_date DESC);
CREATE INDEX IF NOT EXISTS asset_snapshot_daily_date_pe_idx
  ON analytics.asset_snapshot_daily (as_of_date DESC, pe_ttm ASC NULLS LAST);
CREATE INDEX IF NOT EXISTS asset_snapshot_daily_date_growth_idx
  ON analytics.asset_snapshot_daily (as_of_date DESC, forecast_net_profit_growth DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS asset_snapshot_daily_date_actual_profit_growth_idx
  ON analytics.asset_snapshot_daily (as_of_date DESC, actual_net_profit_yoy DESC NULLS LAST)
  WHERE actual_net_profit_yoy IS NOT NULL;

-- Supports the point-in-time snapshot's per-asset latest actual-results lookup.
-- Segment-level and legacy unscoped rows are deliberately excluded.
CREATE INDEX IF NOT EXISTS financial_metrics_company_total_latest_idx
  ON company.financial_statement_metrics (
    asset_id, metric_name, report_date DESC, updated_at DESC, id DESC
  )
  WHERE metadata ->> 'scope' = 'company_total';

INSERT INTO agent.metric_catalog (
  metric_key, display_name, description, canonical_relation, value_columns, grain,
  time_column, available_at_column, dimension_columns, unit, formula, freshness_slo,
  null_policy, lookahead_risk, metadata
)
VALUES
  (
    'asset_snapshot_daily', '股票日度综合快照',
    '按股票和日期汇总技术面、估值、研报预测、评级及综合信号。用于优先筛选，不能替代原始证据。',
    'analytics.asset_snapshot_daily',
    ARRAY['close','pe_ttm','forward_pe','forecast_eps_growth','actual_revenue_yoy','actual_net_profit_yoy','composite_score'],
    '一只股票/一个交易日', 'as_of_date', 'updated_at', ARRAY['stock_code','stock_name'],
    '混合', '由各字段来源按截至 as_of_date 的最近可用快照组合', INTERVAL '2 days',
    '缺失字段保持 NULL，不以零替代', 'point_in_time_required',
    jsonb_build_object('evidence', '需要进一步查询研报或事件原文')
  ),
  (
    'pe_ttm', '滚动市盈率（TTM）',
    '最近十二个月归母净利润口径的静态估值因子。由东方财富日度估值因子的定点整数除以 100 归一化；负值通常表示亏损，低估值筛选默认排除。',
    'analytics.asset_snapshot_daily', ARRAY['pe_ttm'], '一只股票/一个交易日',
    'as_of_date', 'updated_at', ARRAY['stock_code','stock_name'], '倍',
    'market.factors_daily.factor_name=pe_ttm 的 factor_value / 100', INTERVAL '2 days',
    '无行情或无有效利润口径时为 NULL；负值不得解释为低估值', 'point_in_time_required',
    jsonb_build_object('source_relation', 'market.factors_daily', 'source_date_key', 'valuation_date')
  ),
  (
    'pb', '市净率（PB）',
    '市值相对净资产的日度估值因子。由东方财富日度估值因子的定点整数除以 100 归一化。',
    'analytics.asset_snapshot_daily', ARRAY['pb'], '一只股票/一个交易日',
    'as_of_date', 'updated_at', ARRAY['stock_code','stock_name'], '倍',
    'market.factors_daily.factor_name=pb 的 factor_value / 100', INTERVAL '2 days',
    '缺失保持 NULL，不以零替代', 'point_in_time_required',
    jsonb_build_object('source_relation', 'market.factors_daily', 'source_date_key', 'valuation_date')
  ),
  (
    'forward_pe', '研报预测市盈率',
    '明确预测年度的原研报 PE，价格时点是研报日期；不等于最新价格除以 EPS。',
    'analytics.asset_forecast_daily', ARRAY['forward_pe','next_forward_pe'], '一只股票/一个快照日',
    'as_of_date', 'updated_at', ARRAY['stock_code','forecast_year'], '倍',
    '原研报明确年度 PE；报告日期见 forecast_report_date，引用价格可能来自前一收盘', INTERVAL '2 days',
    '无可解析预测时为 NULL，不可回填为 pe_ttm', 'point_in_time_required',
    jsonb_build_object('source_relation', 'research.report_forecasts_usable', 'do_not_compare_as_actual', true)
  ),
  (
    'forecast_eps_growth', '研报预测每股收益增速',
    '同一机构研报对相邻未来年度每股收益（EPS）的预测增速，属于预期数据，不是已披露财报结果。',
    'analytics.asset_forecast_daily', ARRAY['forecast_eps_growth'], '一只股票/一个快照日',
    'as_of_date', 'updated_at', ARRAY['stock_code','forecast_year'], '小数',
    '按可比预测年度计算，0.5 表示 50%', INTERVAL '2 days',
    '预测缺失或不可比时为 NULL', 'point_in_time_required',
    jsonb_build_object('source_relation', 'research.report_forecasts_usable', 'forecast', true)
  ),
  (
    'actual_net_profit_yoy', '已披露归母净利润同比',
    '从财报或公告文本抽取的公司整体归母净利润同比，只使用 metadata.scope=company_total 的实际业绩记录。',
    'company.financial_statement_metrics', ARRAY['metric_yoy','actual_net_profit_yoy'], '一只股票/报告期/来源文档',
    'report_date', 'created_at', ARRAY['stock_code','report_period'], '小数',
    'metric_name=net_profit_parent 且 scope=company_total；0.5 表示 50%', INTERVAL '14 days',
    '无原文证据、分部口径或低置信度时不可用于严格筛选', 'point_in_time_required',
    jsonb_build_object('source_relation', 'company.financial_statement_metrics', 'actual', true)
  ),
  (
    'research_forecast_consensus', '研报预测与一致预期',
    '机构研报中的 EPS、净利润总额、PE、评级和目标价预测；实际财报必须从 financial_statement_metrics 查询。',
    'analytics.asset_forecast_daily',
    ARRAY['forecast_eps','consensus_eps','forecast_net_profit','consensus_net_profit','forward_pe','latest_rating'],
    '一只股票/一个快照日', 'as_of_date', 'updated_at', ARRAY['stock_code','forecast_year'],
    'EPS: CNY/share；净利润: CNY；PE: 倍', '每机构每预测年度取最新一份研报后计算统计量', INTERVAL '2 days',
    '无预测或未解析字段保持 NULL', 'point_in_time_required',
    jsonb_build_object('do_not_use_for', '已披露实际业绩')
  ),
  (
    'reported_financial_growth', '已披露财报增速',
    '从报告或公告文本抽取的实际营收、归母净利润和扣非归母净利润及同比增速；跨股票筛选仅使用 metadata.scope=company_total 的记录。',
    'company.financial_statement_metrics',
    ARRAY['metric_value','metric_yoy'], '一只股票/报告期/指标/来源文档',
    'report_date', 'created_at', ARRAY['stock_code','report_period','metric_name'],
    'CNY/小数', 'metric_yoy 为同比小数，例如 0.5 表示 50%', INTERVAL '14 days',
    '无原文证据、非公司整体口径或低置信度记录不可用于严格筛选', 'point_in_time_required',
    jsonb_build_object('evidence_column', 'source_text')
  ),
  (
    'document_event_fact', '文档事件事实',
    '新闻和研报提炼的公司、行业、政策和需求事件，保留摘要、证据、极性和置信度。',
    'analytics.document_events',
    ARRAY['event_type','polarity','impact_score','confidence'], '一条事件/一个来源文档',
    'event_date', 'updated_at', ARRAY['stock_codes','industries','keywords'],
    '评分', '规则或模型抽取，必须引用 evidence', INTERVAL '3 days',
    '无法确定主体或方向时保留空数组或零极性', 'unknown',
    jsonb_build_object('evidence_column', 'evidence')
  )
ON CONFLICT (metric_key) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  description = EXCLUDED.description,
  canonical_relation = EXCLUDED.canonical_relation,
  value_columns = EXCLUDED.value_columns,
  grain = EXCLUDED.grain,
  time_column = EXCLUDED.time_column,
  available_at_column = EXCLUDED.available_at_column,
  dimension_columns = EXCLUDED.dimension_columns,
  unit = EXCLUDED.unit,
  formula = EXCLUDED.formula,
  freshness_slo = EXCLUDED.freshness_slo,
  null_policy = EXCLUDED.null_policy,
  lookahead_risk = EXCLUDED.lookahead_risk,
  metadata = EXCLUDED.metadata,
  updated_at = now();

INSERT INTO agent.query_catalog (
  query_key, intent, tool_name, description, source_relations, input_schema,
  output_schema, freshness_requirements, evidence_requirements, safety_notes
)
VALUES
  (
    'document_evidence_search', '检索并引用新闻或研报证据', 'finance_document_evidence',
    '最近两天优先 Meilisearch；更长历史或缓存未命中时使用 PostgreSQL FTS，并返回来源文档和日期。',
    ARRAY['corpus.document_search_index','corpus.documents'],
    '{"query":"text","days":"1..3650","source_types":"text[]","stock_code":"optional text"}'::jsonb,
    '{"document_id":"uuid","title":"text","published_date":"date","snippet":"text"}'::jsonb,
    '报告来源和日期必须返回', '结论必须带来源、日期和标题',
    '全文检索不等于事实验证；数值结论需回到原文或结构化事实表。'
  ),
  (
    'stock_point_in_time_screen', '按日期筛选股票综合信号', 'finance_screen_stocks',
    '使用日度快照、主题和研报动量筛选候选股票。',
    ARRAY['analytics.asset_snapshot_daily','analytics.asset_signal_daily','analytics.asset_research_daily'],
    '{"as_of_date":"optional date","pe_lt":"optional number","min_positive_ratings":"optional integer"}'::jsonb,
    '{"stock_code":"text","score":"number","valuation":"number","source_dates":"object"}'::jsonb,
    '必须输出快照日期和各组件实际日期', '入选理由需要原始研报或事件证据',
    '不得把预测指标表述为实际业绩，也不得把候选筛选表述为买卖指令。'
  ),
  (
    'reported_financial_screen', '筛选已披露业绩和报告日收益', 'finance_financial_growth_screen',
    '严格使用已披露财务指标和同源行情收益。',
    ARRAY['company.financial_statement_metrics','analytics.asset_event_return'],
    '{"report_period":"text","metric":"text","growth_gt":"number","return_lte":"number"}'::jsonb,
    '{"stock_code":"text","metric_yoy":"number","return_pct":"number","evidence":"text"}'::jsonb,
    '必须同时返回报告期、报告日和实际行情截止日', '必须返回 source_text 或来源文档',
    '缺失价格或实际指标时不得将零行解释为没有候选。'
  ),
  (
    'temporal_knowledge_graph', '按事实时间和知识时间查询金融关系', 'finance_knowledge_graph',
    '查询公司、行业、机构、评级、预测、事件和产业链关系的历史快照或变更。',
    ARRAY['kg.relations','kg.relation_versions','kg.relation_version_evidence','kg.source_coverage_daily'],
    '{"query":"text","as_of_date":"date","known_at":"timestamptz","knowledge_mode":"public|system","query_mode":"snapshot|history|changes"}'::jsonb,
    '{"relation_type":"text","valid_from":"date","valid_to":"date","public_known_at":"timestamptz","system_known_at":"timestamptz","evidence":"text"}'::jsonb,
    '历史查询必须显式使用 as_of_date/known_at，并报告 coverage_score',
    '每个关系结论必须对应返回的 relation version 和证据文档',
    '缺失关系在覆盖度不足时不代表事实不存在；默认 public 模式，禁止把今日图谱状态回填到历史。'
  )
ON CONFLICT (query_key) DO UPDATE SET
  intent = EXCLUDED.intent,
  tool_name = EXCLUDED.tool_name,
  description = EXCLUDED.description,
  source_relations = EXCLUDED.source_relations,
  input_schema = EXCLUDED.input_schema,
  output_schema = EXCLUDED.output_schema,
  freshness_requirements = EXCLUDED.freshness_requirements,
  evidence_requirements = EXCLUDED.evidence_requirements,
  safety_notes = EXCLUDED.safety_notes,
  updated_at = now();

INSERT INTO agent.query_catalog (
  query_key, intent, tool_name, description, source_relations, input_schema,
  output_schema, freshness_requirements, evidence_requirements, safety_notes
)
VALUES
  (
    'finance_data_context', '查询指标口径、数据范围、覆盖率和鲜度', 'finance_data_context',
    '返回 Agent 指标目录、批准的查询意图、数据契约和固定数据集健康检查。',
    ARRAY['agent.metric_catalog','agent.query_catalog','agent.data_contracts'],
    '{"intent":"optional text","metric_keys":"optional text[]","dataset_keys":"optional text[]","query_keys":"optional text[]"}'::jsonb,
    '{"context_type":"metric|query|contract|health","context_key":"text","details":"jsonb"}'::jsonb,
    '回答时必须使用 health 或目录中的 freshness_slo 判断鲜度',
    '数值结论仍需调用对应业务工具或文档证据工具',
    '语义目录描述口径和范围，不等于业务事实结果。'
  ),
  (
    'full_market_point_in_time_screen', '全市场估值、成长、研报和技术面筛选', 'finance_market_screen',
    '使用全市场日度快照按指定日期筛选 PE TTM、PB、forward PE、预测增速、实际业绩增速及收益。',
    ARRAY['analytics.asset_snapshot_daily','market.assets','kg.company_concept_exposures','kg.concepts'],
    '{"scope":"all|theme","as_of_date":"optional date","pe_ttm_lt":"optional number","forecast_growth_gte":"optional number","actual_profit_growth_gte":"optional number"}'::jsonb,
    '{"stock_code":"text","as_of_date":"date","pe_ttm":"number","forecast_eps_growth":"number","actual_net_profit_yoy":"number","source_dates":"jsonb"}'::jsonb,
    '必须返回实际快照日期和 source_dates',
    '需要定性解释时，再检索研报或新闻原文',
    '不得混淆 pe_ttm 与 forward_pe，也不得把 forecast growth 表述为实际业绩。'
  )
ON CONFLICT (query_key) DO UPDATE SET
  intent = EXCLUDED.intent,
  tool_name = EXCLUDED.tool_name,
  description = EXCLUDED.description,
  source_relations = EXCLUDED.source_relations,
  input_schema = EXCLUDED.input_schema,
  output_schema = EXCLUDED.output_schema,
  freshness_requirements = EXCLUDED.freshness_requirements,
  evidence_requirements = EXCLUDED.evidence_requirements,
  safety_notes = EXCLUDED.safety_notes,
  updated_at = now();

INSERT INTO agent.data_contracts (
  dataset_key, relation_name, grain, primary_key_columns, watermark_column,
  freshness_slo, required_columns, quality_checks
)
VALUES
  (
    'document_search', 'corpus.document_search_index', '一篇文档一行', ARRAY['document_id'],
    'indexed_at', INTERVAL '1 hour', ARRAY['document_id','source_type','search_text'],
    '["PostgreSQL FTS 覆盖 corpus.documents 全历史", "Meilisearch 最近两天 published_at 文档完整且可检索", "indexed_at 不落后 source_updated_at"]'::jsonb
  ),
  (
    'asset_snapshot', 'analytics.asset_snapshot_daily', '一只股票一个快照日', ARRAY['asset_id','as_of_date'],
    'updated_at', INTERVAL '2 days', ARRAY['asset_id','stock_code','as_of_date','source_dates'],
    '["as_of_date 不晚于当前日期", "source_dates 必须披露技术、预测、研报日期"]'::jsonb
  ),
  (
    'research_reports', 'research.reports', '一篇研报一行', ARRAY['document_id'],
    'updated_at', INTERVAL '2 days', ARRAY['report_type','report_date','ocr_status'],
    '["eastmoney_infocode 唯一", "OCR 完成后必须进入文档索引队列"]'::jsonb
  ),
  (
    'temporal_knowledge_graph', 'kg.relation_versions', '一个关系的一个事实/知识时间版本', ARRAY['relation_id','version_key'],
    'created_at', INTERVAL '2 days',
    ARRAY['relation_id','version_key','valid_during','public_known_during','system_known_during','confidence','source_system'],
    '["valid_during 非空", "public/system known range 非空", "事实结论有 relation_version_evidence", "按 source_coverage_daily 披露覆盖状态"]'::jsonb
  ),
  (
    'temporal_graph_factors', 'analytics.graph_feature_daily', '一只股票一个交易日', ARRAY['asset_id','dt'],
    'updated_at', INTERVAL '2 days', ARRAY['asset_id','dt','coverage_status','source_coverage_ratio','available_at'],
    '["新增图因子覆盖不足时为 NULL", "available_at 不晚于因子使用时间", "coverage_status 必须随结果披露"]'::jsonb
  )
ON CONFLICT (dataset_key) DO UPDATE SET
  relation_name = EXCLUDED.relation_name,
  grain = EXCLUDED.grain,
  primary_key_columns = EXCLUDED.primary_key_columns,
  watermark_column = EXCLUDED.watermark_column,
  freshness_slo = EXCLUDED.freshness_slo,
  required_columns = EXCLUDED.required_columns,
  quality_checks = EXCLUDED.quality_checks,
  updated_at = now();

GRANT USAGE ON SCHEMA agent TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA agent TO finance_app;
GRANT USAGE ON SCHEMA agent TO finance_ai_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA agent TO finance_ai_reader;
GRANT USAGE ON SCHEMA analytics TO finance_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON analytics.asset_snapshot_daily TO finance_app;
GRANT SELECT ON analytics.asset_snapshot_daily TO finance_ai_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA agent
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO finance_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA agent
  GRANT SELECT ON TABLES TO finance_ai_reader;

COMMIT;
