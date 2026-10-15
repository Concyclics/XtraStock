BEGIN;

CREATE TABLE IF NOT EXISTS factor.factor_health_daily (
  factor_id uuid NOT NULL REFERENCES factor.definitions(factor_id) ON DELETE CASCADE,
  dt date NOT NULL,
  lifecycle_status text NOT NULL,
  score_rows bigint NOT NULL,
  scored_assets integer NOT NULL,
  eligible_assets integer NOT NULL,
  coverage double precision NOT NULL,
  zero_value_ratio double precision,
  raw_mean double precision,
  raw_stddev double precision,
  raw_min double precision,
  raw_max double precision,
  latest_available_at timestamptz,
  source_feature_date date,
  computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (factor_id, dt),
  CONSTRAINT factor_health_lifecycle_chk
    CHECK (lifecycle_status IN ('candidate', 'shadow', 'production', 'retired')),
  CONSTRAINT factor_health_coverage_chk CHECK (coverage >= 0 AND coverage <= 1)
);
CREATE INDEX IF NOT EXISTS factor_health_daily_dt_idx
  ON factor.factor_health_daily (dt DESC, lifecycle_status, factor_id);

CREATE TABLE IF NOT EXISTS factor.production_approvals (
  approval_id bigserial PRIMARY KEY,
  factor_id uuid NOT NULL REFERENCES factor.definitions(factor_id) ON DELETE CASCADE,
  factor_version integer NOT NULL,
  evaluation_run_id uuid NOT NULL REFERENCES factor.experiment_runs(run_id) ON DELETE RESTRICT,
  decision text NOT NULL,
  approved_by text NOT NULL,
  reason text NOT NULL,
  effective_date date NOT NULL,
  expires_at timestamptz,
  checklist jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT factor_production_decision_chk CHECK (decision IN ('approved','rejected','revoked'))
);
CREATE INDEX IF NOT EXISTS factor_production_approvals_factor_idx
  ON factor.production_approvals (factor_id, created_at DESC);

UPDATE factor.definitions
SET promotion_gates = promotion_gates || jsonb_build_object(
      'min_rank_ic_ir', 0.20,
      'min_top_bottom_spread', 0.0,
      'max_turnover', 0.50
    ),
    updated_at = now()
WHERE NOT promotion_gates ? 'min_rank_ic_ir'
   OR NOT promotion_gates ? 'min_top_bottom_spread'
   OR NOT promotion_gates ? 'max_turnover';

GRANT SELECT, INSERT, UPDATE, DELETE ON factor.factor_health_daily, factor.production_approvals TO finance_app;
GRANT USAGE, SELECT, UPDATE ON SEQUENCE factor.production_approvals_approval_id_seq TO finance_app;
GRANT SELECT ON factor.factor_health_daily, factor.production_approvals TO finance_ai_reader;

INSERT INTO agent.metric_catalog (
  metric_key,display_name,description,canonical_relation,value_columns,grain,
  time_column,available_at_column,dimension_columns,unit,formula,freshness_slo,
  null_policy,lookahead_risk,status,metadata
) VALUES (
  'factor_health_daily','因子每日健康度',
  '交互式因子目录和生产就绪检查使用的预聚合覆盖、方差、零值率和更新时间。',
  'factor.factor_health_daily',ARRAY['coverage','zero_value_ratio','raw_stddev','scored_assets'],
  '一个因子/一个交易日','dt','latest_available_at',ARRAY['factor_id'],'mixed',
  '由 factor.daily_scores 按因子和交易日聚合',INTERVAL '2 days',
  '缺失表示健康汇总尚未运行，不得回退到交互式全表扫描','point_in_time_required','active',
  '{"interactive_source":true,"production_gate":true}'::jsonb
) ON CONFLICT (metric_key) DO UPDATE SET
  description=EXCLUDED.description,canonical_relation=EXCLUDED.canonical_relation,
  value_columns=EXCLUDED.value_columns,freshness_slo=EXCLUDED.freshness_slo,
  null_policy=EXCLUDED.null_policy,metadata=EXCLUDED.metadata,updated_at=now();

INSERT INTO agent.query_catalog (
  query_key,intent,tool_name,description,source_relations,input_schema,output_schema,
  freshness_requirements,evidence_requirements,safety_notes,status
) VALUES
  ('factor_readiness','判断因子是否可用于生产预测','finance_factor_readiness',
   '检查健康、独立样本外窗口、换手、分组收益、审批和显式阻塞项。',
   ARRAY['factor.definitions','factor.factor_health_daily','factor.evaluations','factor.experiment_runs','factor.production_approvals'],
   '{"optional":["factor_name","family","lifecycle_status"]}'::jsonb,
   '{"requires":["prediction_eligible","blockers","passing_oos_windows","latest_coverage"]}'::jsonb,
   '健康表不超过2个交易日','必须披露测试窗、换手、收益差和审批状态',
   '只有prediction_eligible=true可支持生产预测性描述','active'),
  ('factor_attribution','解释单股因子贡献和冲突','finance_factor_attribution',
   '返回单股同日因子方向、横截面分位、健康和样本外状态。',
   ARRAY['factor.daily_scores','factor.definitions','factor.factor_health_daily','factor.evaluations'],
   '{"required_any":["stock_code","stock_name"],"optional":["as_of_date","factor_names"]}'::jsonb,
   '{"requires":["score_date","percentile","zscore","prediction_eligible"]}'::jsonb,
   '评分和健康表使用同一交易日','每个贡献必须带生命周期和验证状态',
   '贡献是解释，不是上涨概率；冲突信号必须同时披露','active')
ON CONFLICT (query_key) DO UPDATE SET
  intent=EXCLUDED.intent,tool_name=EXCLUDED.tool_name,description=EXCLUDED.description,
  source_relations=EXCLUDED.source_relations,input_schema=EXCLUDED.input_schema,
  output_schema=EXCLUDED.output_schema,freshness_requirements=EXCLUDED.freshness_requirements,
  evidence_requirements=EXCLUDED.evidence_requirements,safety_notes=EXCLUDED.safety_notes,
  status=EXCLUDED.status,updated_at=now();

UPDATE agent.query_catalog
SET source_relations=ARRAY['factor.definitions','factor.factor_health_daily','factor.evaluations','factor.experiment_runs'],
    safety_notes='目录必须读取预聚合健康表；candidate/shadow不支持生产预测',updated_at=now()
WHERE query_key='factor_catalog';

UPDATE agent.query_catalog
SET input_schema=input_schema || '{"quality_filters":["review_statuses","directness","source_systems","require_evidence","exclude_event_relations"]}'::jsonb,
    safety_notes='路径是关联而非因果；unreviewed边只能作为候选；供应链查询应排除事件边并要求证据',updated_at=now()
WHERE query_key='temporal_graph_paths';

INSERT INTO agent.data_contracts (
  dataset_key,relation_name,grain,primary_key_columns,watermark_column,freshness_slo,
  required_columns,quality_checks,status
) VALUES (
  'factor_health','factor.factor_health_daily','一个因子一个交易日',
  ARRAY['factor_id','dt'],'latest_available_at',INTERVAL '2 days',
  ARRAY['factor_id','dt','coverage','scored_assets','raw_stddev','latest_available_at'],
  '["coverage在0到1之间","dt应追平canonical行情","raw_stddev为零时不可生产"]'::jsonb,'active'
) ON CONFLICT (dataset_key) DO UPDATE SET
  relation_name=EXCLUDED.relation_name,grain=EXCLUDED.grain,
  primary_key_columns=EXCLUDED.primary_key_columns,watermark_column=EXCLUDED.watermark_column,
  freshness_slo=EXCLUDED.freshness_slo,required_columns=EXCLUDED.required_columns,
  quality_checks=EXCLUDED.quality_checks,status=EXCLUDED.status,updated_at=now();

COMMIT;
