BEGIN;

CREATE TABLE IF NOT EXISTS analytics.factor_score_latest (
  factor_id uuid NOT NULL REFERENCES factor.definitions(factor_id) ON DELETE CASCADE,
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  dt date NOT NULL,
  raw_value double precision,
  percentile double precision,
  zscore double precision,
  lifecycle_status text NOT NULL,
  available_at timestamptz NOT NULL,
  source_feature_date date NOT NULL,
  refreshed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (factor_id,asset_id)
);

CREATE INDEX IF NOT EXISTS factor_score_latest_asset_idx
  ON analytics.factor_score_latest (asset_id,dt DESC,percentile DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS factor_score_latest_factor_rank_idx
  ON analytics.factor_score_latest (factor_id,lifecycle_status,percentile DESC NULLS LAST,asset_id);

CREATE TABLE IF NOT EXISTS analytics.graph_adjacency_latest (
  relation_version_id uuid PRIMARY KEY REFERENCES kg.relation_versions(relation_version_id) ON DELETE CASCADE,
  relation_id uuid NOT NULL REFERENCES kg.relations(relation_id) ON DELETE CASCADE,
  source_entity_id uuid NOT NULL REFERENCES kg.market_entities(entity_id) ON DELETE CASCADE,
  target_entity_id uuid NOT NULL REFERENCES kg.market_entities(entity_id) ON DELETE CASCADE,
  relation_type text NOT NULL,
  valid_from date NOT NULL,
  valid_to date,
  public_known_from timestamptz NOT NULL,
  confidence double precision NOT NULL,
  review_status text NOT NULL,
  claim_class text NOT NULL,
  source_system text NOT NULL,
  source_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  refreshed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS graph_adjacency_latest_source_idx
  ON analytics.graph_adjacency_latest (source_entity_id,relation_type,confidence DESC);
CREATE INDEX IF NOT EXISTS graph_adjacency_latest_target_idx
  ON analytics.graph_adjacency_latest (target_entity_id,relation_type,confidence DESC);
CREATE INDEX IF NOT EXISTS graph_adjacency_latest_review_idx
  ON analytics.graph_adjacency_latest (claim_class,review_status,confidence DESC);

CREATE INDEX IF NOT EXISTS factor_lifecycle_events_factor_idx
  ON factor.lifecycle_events (factor_id,created_at DESC);

CREATE INDEX IF NOT EXISTS canonical_bars_v2_asset_date_cover_idx
  ON market.canonical_bars_daily_v2 (asset_id,dt DESC)
  INCLUDE (open,high,low,close,volume,amount,source,available_at,quality_status);
CREATE INDEX IF NOT EXISTS trading_calendar_open_date_idx
  ON market.trading_calendar (trade_date DESC,exchange)
  WHERE session_status='open';
CREATE INDEX IF NOT EXISTS corporate_actions_asset_exdate_idx
  ON market.corporate_actions (asset_id,ex_date DESC,available_at DESC)
  WHERE quality_status NOT IN ('rejected','conflict');
CREATE INDEX IF NOT EXISTS filings_source_type_announced_idx
  ON company.filings (source_name,filing_type,announced_at DESC)
  INCLUDE (asset_id,stock_code,period_end,available_at,revision_no,document_id,quality_status);
CREATE INDEX IF NOT EXISTS financial_facts_pit_lookup_idx
  ON company.financial_facts (asset_id,fact_kind,metric_code,available_at DESC,period_end DESC)
  INCLUDE (value,yoy,unit,revision_no,quality_status,source_document_id);
CREATE INDEX IF NOT EXISTS document_quality_bad_idx
  ON corpus.document_quality (checked_at DESC,document_id)
  WHERE quality_status IN ('template_noise','parse_failed','duplicate','quarantined');
CREATE INDEX IF NOT EXISTS message_outbox_retry_idx
  ON agent.message_outbox (channel,available_at,priority,created_at)
  WHERE status IN ('pending','failed','sending');
CREATE INDEX IF NOT EXISTS quality_results_dataset_checked_idx
  ON ops.quality_results_v3 (dataset_key,partition_key,checked_at DESC)
  INCLUDE (check_name,status,severity,observed_value,affected_rows);

CREATE TABLE IF NOT EXISTS ops.backfill_partitions_v3 (
  job_name text NOT NULL,
  partition_start date NOT NULL,
  partition_end date NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  attempt_count integer NOT NULL DEFAULT 0,
  started_at timestamptz,
  completed_at timestamptz,
  row_count bigint,
  error_message text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_name,partition_start,partition_end),
  CONSTRAINT backfill_partitions_v3_status_chk
    CHECK (status IN ('pending','running','succeeded','failed','skipped'))
);
CREATE INDEX IF NOT EXISTS backfill_partitions_v3_status_idx
  ON ops.backfill_partitions_v3 (job_name,status,partition_start);

CREATE TABLE IF NOT EXISTS agent.access_defaults (
  channel text NOT NULL,
  subject_type text NOT NULL,
  default_daily_budget integer NOT NULL DEFAULT 0,
  default_min_interval_seconds integer NOT NULL DEFAULT 0,
  whitelist_enabled boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (channel,subject_type),
  CONSTRAINT access_defaults_channel_chk CHECK (channel IN ('qq','wechat')),
  CONSTRAINT access_defaults_subject_chk CHECK (subject_type IN ('user','group')),
  CONSTRAINT access_defaults_nonnegative_chk CHECK (
    default_daily_budget >= 0 AND default_min_interval_seconds >= 0
  )
);

GRANT SELECT,INSERT,UPDATE,DELETE ON agent.access_defaults TO finance_app;
GRANT SELECT ON agent.access_defaults TO finance_ai_reader;
GRANT SELECT,INSERT,UPDATE,DELETE ON ops.backfill_partitions_v3 TO finance_app;
GRANT SELECT ON ops.backfill_partitions_v3 TO finance_ai_reader;

ALTER TABLE market.canonical_bars_daily_v2 SET (
  autovacuum_analyze_scale_factor=0.02,
  autovacuum_vacuum_scale_factor=0.05,
  autovacuum_analyze_threshold=10000,
  autovacuum_vacuum_threshold=20000
);
ALTER TABLE kg.relation_versions SET (
  autovacuum_analyze_scale_factor=0.02,
  autovacuum_vacuum_scale_factor=0.05
);
ALTER TABLE company.financial_facts SET (
  autovacuum_analyze_scale_factor=0.03,
  autovacuum_vacuum_scale_factor=0.08
);

INSERT INTO agent.metric_catalog (
  metric_key,display_name,description,canonical_relation,value_columns,grain,
  time_column,available_at_column,dimension_columns,unit,formula,freshness_slo,
  null_policy,lookahead_risk,status,metadata
) VALUES
('canonical_raw_ohlcv_v2','Canonical V2 未复权日线','东方财富未复权主源、Sina未复权备用源；禁止混入前复权价格。',
 'market.canonical_bars_daily_v2',ARRAY['open','high','low','close','volume','amount'],
 '一只资产一个交易日','dt','available_at',ARRAY['asset_id','source','quality_status'],'source-specific',NULL,
 INTERVAL '2 days','缺失表示该资产交易状态或来源数据不足，不得填零','point_in_time_required','active',
 '{"price_basis":"raw_unadjusted","allowed_adjustment":"none"}'::jsonb),
('canonical_total_return_v2','Canonical V2 总回报派生序列','由未复权价格和点时公司行为复权因子生成，供技术分析与因子使用。',
 'market.canonical_total_return_daily_v2',ARRAY['open','high','low','close'],
 '一只资产一个交易日','dt','factor_available_at',ARRAY['asset_id','source','quality_status'],'CNY',
 'raw_price * total_return_factor',INTERVAL '2 days','公司行为不完整时质量状态必须降级','point_in_time_required','active',
 '{"derived":true,"not_source_price":true}'::jsonb),
('typed_financial_fact','强类型财务事实','actual、forecast、consensus、report_claim 强类型隔离并保留披露及可得时间。',
 'company.financial_facts',ARRAY['value','yoy'],'一只资产一个指标一个口径一个报告期一个修订版本',
 'period_end','available_at',ARRAY['asset_id','metric_code','fact_kind','statement_scope','revision_no'],'metric-specific',NULL,
 INTERVAL '120 days','缺失不得按零处理；不同 fact_kind 禁止合并','point_in_time_required','active',
 '{"fact_kinds":["actual","forecast","consensus","report_claim"],"mixing_forbidden":true}'::jsonb),
('document_quality_v3','文档质量门禁','正文长度、模板相似、重复、解析状态和隔离结果。',
 'corpus.document_quality',ARRAY['quality_score','body_chars','template_similarity'],'一篇文档',
 'checked_at','checked_at',ARRAY['document_id','quality_status'],'score',NULL,INTERVAL '1 day',
 '缺失表示尚未评分，不得进入正式证据集','none','active','{"quarantine_enforced":true}'::jsonb)
ON CONFLICT (metric_key) DO UPDATE SET
  display_name=EXCLUDED.display_name,description=EXCLUDED.description,
  canonical_relation=EXCLUDED.canonical_relation,value_columns=EXCLUDED.value_columns,
  grain=EXCLUDED.grain,time_column=EXCLUDED.time_column,
  available_at_column=EXCLUDED.available_at_column,dimension_columns=EXCLUDED.dimension_columns,
  unit=EXCLUDED.unit,formula=EXCLUDED.formula,freshness_slo=EXCLUDED.freshness_slo,
  null_policy=EXCLUDED.null_policy,lookahead_risk=EXCLUDED.lookahead_risk,
  status=EXCLUDED.status,metadata=EXCLUDED.metadata,updated_at=now();

INSERT INTO agent.data_contracts (
  dataset_key,relation_name,grain,primary_key_columns,watermark_column,freshness_slo,
  required_columns,quality_checks,owner,status
) VALUES
('market.trading_calendar','market.trading_calendar','交易所一个自然日',ARRAY['exchange','trade_date'],'available_at',INTERVAL '1 day',
 ARRAY['exchange','trade_date','session_status','available_at','is_verified'],
 '["近期工作日必须由官方日历或市场观测确认","官方休市与市场观测冲突为硬失败"]'::jsonb,'market_pipeline','active'),
('market.canonical_bars_daily_v2','market.canonical_bars_daily_v2','一只资产一个交易日',ARRAY['asset_id','dt'],'available_at',INTERVAL '2 days',
 ARRAY['asset_id','dt','close','source','adjustment','price_basis','available_at','quality_status'],
 '["adjustment只能为none","price_basis只能为raw_unadjusted","OHLC合法","预期可交易股票覆盖率>=99%","跨源偏差受控"]'::jsonb,'market_pipeline','active'),
('company.financial_facts','company.financial_facts','一只资产一个事实口径报告期修订版本',ARRAY['financial_fact_id'],'available_at',INTERVAL '120 days',
 ARRAY['asset_id','metric_code','fact_kind','period_end','value','unit','available_at','source_name','quality_status'],
 '["fact_kind强类型隔离","available_at不得早于公开披露","同口径修订版本唯一"]'::jsonb,'filing_pipeline','active'),
('corpus.document_quality','corpus.document_quality','一篇文档',ARRAY['document_id'],'checked_at',INTERVAL '1 day',
 ARRAY['document_id','quality_status','quality_score','body_chars','checked_at'],
 '["模板噪声不得进入检索","正文指纹重复只保留一份","短正文进入重解析队列"]'::jsonb,'search_pipeline','active'),
('agent.message_outbox','agent.message_outbox','一个目标的一条幂等消息',ARRAY['message_id'],'updated_at',INTERVAL '5 minutes',
 ARRAY['channel','target_type','target_id','dedup_key','payload','status','available_at'],
 '["dedup_key唯一","发送失败指数退避","sending租约超时可恢复"]'::jsonb,'agent_runtime','active'),
('agent.daily_data_gate','ops.dataset_watermarks','一个报告日的一次统一门禁结果',ARRAY['dataset_key','partition_key'],'completed_at',INTERVAL '1 day',
 ARRAY['dataset_key','expected_date','data_through','coverage_pct','quality_status','completed_at'],
 '["所有日报必需数据集有水位","硬失败数据集阻断投资建议","门禁日期等于报告日"]'::jsonb,'pipeline_controller','active')
ON CONFLICT (dataset_key) DO UPDATE SET
 relation_name=EXCLUDED.relation_name,grain=EXCLUDED.grain,
 primary_key_columns=EXCLUDED.primary_key_columns,watermark_column=EXCLUDED.watermark_column,
 freshness_slo=EXCLUDED.freshness_slo,required_columns=EXCLUDED.required_columns,
 quality_checks=EXCLUDED.quality_checks,owner=EXCLUDED.owner,status=EXCLUDED.status,updated_at=now();

INSERT INTO agent.query_catalog (
  query_key,intent,tool_name,description,source_relations,input_schema,output_schema,
  freshness_requirements,evidence_requirements,safety_notes,status
) VALUES
('v3_market_series_batch','批量读取点时行情','finance_market_series_batch','读取未复权或总回报 Canonical V2，多股票必须批量。',
 ARRAY['market.canonical_bars_daily_v2','market.adjustment_factors_daily'],
 '{"required":["stock_codes"],"optional":["start_date","end_date","price_basis","decision_cutoff"]}'::jsonb,
 '{"protocol":"meta_rows_evidence","claim_type":"market"}'::jsonb,'行情水位不超过2个交易日','每行必须带来源、日期、available_at和evidence_id','禁止混用raw_unadjusted和total_return','active'),
('v3_fundamentals_batch','批量读取强类型财务事实','finance_fundamentals_batch','默认只读actual；其他事实类型必须显式请求。',
 ARRAY['company.financial_facts','company.filings'],
 '{"required":["stock_codes"],"optional":["fact_kinds","metric_codes","periods","decision_cutoff"]}'::jsonb,
 '{"protocol":"meta_rows_evidence","typed_fact_kind":true}'::jsonb,'正式披露按报告期和披露日更新','每个数值绑定period_end、available_at、unit、source','actual/forecast/consensus/report_claim禁止混查后合并','active'),
('v3_document_retrieve','独立三路召回并RRF融合','finance_document_retrieve','Meili、Postgres FTS、向量独立召回，RRF融合。',
 ARRAY['corpus.document_search_index','corpus.document_embeddings','corpus.document_quality'],
 '{"required":["query"],"optional":["days","source_types","stock_code","limit"]}'::jsonb,
 '{"protocol":"meta_rows_evidence","retrieval_channels":["meilisearch","postgres_fts","semantic"]}'::jsonb,
 '文档索引不超过1小时','证据必须绑定document_id、来源和公开日期','检索排名不是事实验证；隔离文档不得返回','active'),
('v3_factor_analysis','读取生产治理后的因子状态','finance_factor_analysis','默认排除candidate并返回生命周期。',
 ARRAY['analytics.factor_score_latest','factor.factor_health_daily','factor.definitions'],
 '{"optional":["stock_codes","factor_names","as_of_date","include_candidate"]}'::jsonb,
 '{"protocol":"meta_rows_evidence","claim_type":"model_signal"}'::jsonb,'评分水位不超过2个交易日','必须带生命周期、日期、available_at和版本','candidate/shadow/degraded不是生产建议','active'),
('v3_graph_analysis','读取双时态有证据图谱','finance_graph_analysis','返回世界时间、知识时间、来源、审核状态和claim_class。',
 ARRAY['kg.relations','kg.relation_versions','kg.relation_version_evidence'],
 '{"optional":["query","stock_codes","relation_types","as_of_date","decision_cutoff"]}'::jsonb,
 '{"protocol":"meta_rows_evidence","claim_classes":["observed_fact","extracted_claim","model_inference"]}'::jsonb,
 '图谱水位不超过2天','每条边必须保留source和review_status','路径是关联而非因果；非官方结构化来源不可自动成为verified fact','active')
ON CONFLICT (query_key) DO UPDATE SET
 intent=EXCLUDED.intent,tool_name=EXCLUDED.tool_name,description=EXCLUDED.description,
 source_relations=EXCLUDED.source_relations,input_schema=EXCLUDED.input_schema,
 output_schema=EXCLUDED.output_schema,freshness_requirements=EXCLUDED.freshness_requirements,
 evidence_requirements=EXCLUDED.evidence_requirements,safety_notes=EXCLUDED.safety_notes,
 status=EXCLUDED.status,updated_at=now();

GRANT SELECT,INSERT,UPDATE,DELETE ON analytics.factor_score_latest,analytics.graph_adjacency_latest,factor.lifecycle_events TO finance_app;
GRANT SELECT ON analytics.factor_score_latest,analytics.graph_adjacency_latest,factor.lifecycle_events TO finance_ai_reader;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA factor TO finance_app;

-- Repair historical CNINFO classifications created before report-title suffix
-- matching was introduced. Related notices and inquiry replies remain announcements.
UPDATE company.filings
SET filing_type = CASE
      WHEN title ~ '半年度报告(摘要)?([（(]修订版?[）)])?$' THEN 'semiannual_report'
      WHEN title ~ '年度报告(摘要)?([（(]修订版?[）)])?$' THEN 'annual_report'
      WHEN title ~ '(第一|第三)?季度报告(摘要)?([（(]修订版?[）)])?$' THEN 'quarterly_report'
      WHEN title ~ '(年度|半年度)?权益分派实施公告' THEN 'corporate_action'
      WHEN title ~ '业绩快报(公告)?$' THEN 'earnings_flash'
      WHEN title ~ '业绩预告(公告)?$' THEN 'earnings_forecast'
      ELSE 'announcement'
    END,
    updated_at = now()
WHERE source_name='cninfo'
  AND filing_type IS DISTINCT FROM CASE
      WHEN title ~ '半年度报告(摘要)?([（(]修订版?[）)])?$' THEN 'semiannual_report'
      WHEN title ~ '年度报告(摘要)?([（(]修订版?[）)])?$' THEN 'annual_report'
      WHEN title ~ '(第一|第三)?季度报告(摘要)?([（(]修订版?[）)])?$' THEN 'quarterly_report'
      WHEN title ~ '(年度|半年度)?权益分派实施公告' THEN 'corporate_action'
      WHEN title ~ '业绩快报(公告)?$' THEN 'earnings_flash'
      WHEN title ~ '业绩预告(公告)?$' THEN 'earnings_forecast'
      ELSE 'announcement'
    END;

INSERT INTO ops.schema_migrations (migration_id,description,checksum,metadata)
VALUES ('20260810_finance_agent_v3_runtime','V3 runtime preaggregations, indexes and active semantic contracts','manual:v3','{"destructive":false}'::jsonb)
ON CONFLICT (migration_id) DO UPDATE SET description=EXCLUDED.description,checksum=EXCLUDED.checksum,metadata=EXCLUDED.metadata,applied_at=now();

COMMIT;
