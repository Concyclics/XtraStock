BEGIN;

CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS agent;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS company;
CREATE SCHEMA IF NOT EXISTS corpus;
CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE IF NOT EXISTS ops.schema_migrations (
  migration_id text PRIMARY KEY,
  description text NOT NULL,
  checksum text,
  applied_at timestamptz NOT NULL DEFAULT now(),
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS market.trading_calendar (
  exchange text NOT NULL,
  trade_date date NOT NULL,
  session_status text NOT NULL,
  open_at timestamptz,
  close_at timestamptz,
  source_name text NOT NULL,
  source_url text,
  observed_at timestamptz NOT NULL DEFAULT now(),
  available_at timestamptz NOT NULL DEFAULT now(),
  is_verified boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (exchange, trade_date),
  CONSTRAINT trading_calendar_exchange_chk
    CHECK (exchange IN ('SSE','SZSE','BSE','CFFEX','SHFE','DCE','CZCE','INE')),
  CONSTRAINT trading_calendar_status_chk
    CHECK (session_status IN ('open','closed','half_day','unknown'))
);

CREATE INDEX IF NOT EXISTS trading_calendar_date_idx
  ON market.trading_calendar (trade_date DESC, exchange, session_status);

CREATE TABLE IF NOT EXISTS market.corporate_actions (
  action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  action_type text NOT NULL,
  announced_at timestamptz,
  record_date date,
  ex_date date NOT NULL,
  pay_date date,
  split_ratio double precision,
  stock_dividend_ratio double precision,
  cash_dividend double precision,
  rights_ratio double precision,
  rights_price double precision,
  currency text NOT NULL DEFAULT 'CNY',
  source_name text NOT NULL,
  source_url text,
  source_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  source_external_id text,
  revision_no integer NOT NULL DEFAULT 1,
  available_at timestamptz NOT NULL,
  quality_status text NOT NULL DEFAULT 'unverified',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT corporate_actions_type_chk CHECK (
    action_type IN ('cash_dividend','stock_dividend','split','reverse_split','rights_issue','delisting','other')
  ),
  CONSTRAINT corporate_actions_quality_chk
    CHECK (quality_status IN ('verified','unverified','conflict','rejected')),
  CONSTRAINT corporate_actions_revision_chk CHECK (revision_no > 0),
  CONSTRAINT corporate_actions_source_unique
    UNIQUE (source_name, source_external_id, revision_no)
);

CREATE INDEX IF NOT EXISTS corporate_actions_asset_ex_idx
  ON market.corporate_actions (asset_id, ex_date DESC, available_at DESC);
CREATE INDEX IF NOT EXISTS corporate_actions_document_idx
  ON market.corporate_actions (source_document_id)
  WHERE source_document_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS market.adjustment_factors_daily (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  dt date NOT NULL,
  split_factor double precision NOT NULL DEFAULT 1.0,
  total_return_factor double precision NOT NULL DEFAULT 1.0,
  source_version text NOT NULL DEFAULT 'corporate_actions:v1',
  available_at timestamptz NOT NULL,
  quality_status text NOT NULL DEFAULT 'unverified',
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, dt),
  CONSTRAINT adjustment_factors_positive_chk
    CHECK (split_factor > 0 AND total_return_factor > 0),
  CONSTRAINT adjustment_factors_quality_chk
    CHECK (quality_status IN ('verified','unverified','conflict','incomplete'))
);

CREATE INDEX IF NOT EXISTS adjustment_factors_date_idx
  ON market.adjustment_factors_daily (dt DESC, asset_id);

CREATE TABLE IF NOT EXISTS market.canonical_bars_daily_v2 (
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  dt date NOT NULL,
  open double precision,
  high double precision,
  low double precision,
  close double precision NOT NULL,
  volume double precision,
  amount double precision,
  source text NOT NULL,
  adjustment text NOT NULL DEFAULT 'none',
  price_basis text NOT NULL DEFAULT 'raw_unadjusted',
  source_ts timestamptz,
  source_updated_at timestamptz,
  is_fallback boolean NOT NULL DEFAULT false,
  available_at timestamptz NOT NULL,
  quality_status text NOT NULL DEFAULT 'unverified',
  quality jsonb NOT NULL DEFAULT '{}'::jsonb,
  source_version text NOT NULL DEFAULT 'canonical_daily:v2',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (asset_id, dt),
  CONSTRAINT canonical_bars_v2_adjustment_chk CHECK (adjustment = 'none'),
  CONSTRAINT canonical_bars_v2_basis_chk CHECK (price_basis = 'raw_unadjusted'),
  CONSTRAINT canonical_bars_v2_quality_chk
    CHECK (quality_status IN ('verified','unverified','conflict','incomplete','rejected')),
  CONSTRAINT canonical_bars_v2_ohlc_chk CHECK (
    open IS NULL OR high IS NULL OR low IS NULL OR
    (high >= greatest(open, close) AND low <= least(open, close) AND low >= 0)
  )
);

CREATE INDEX IF NOT EXISTS canonical_bars_v2_date_idx
  ON market.canonical_bars_daily_v2 (dt DESC, quality_status, asset_id);
CREATE INDEX IF NOT EXISTS canonical_bars_v2_source_idx
  ON market.canonical_bars_daily_v2 (source, dt DESC, asset_id);
CREATE INDEX IF NOT EXISTS canonical_bars_v2_quality_fail_idx
  ON market.canonical_bars_daily_v2 (dt DESC, asset_id)
  WHERE quality_status IN ('conflict','incomplete','rejected');

CREATE OR REPLACE VIEW market.canonical_total_return_daily_v2 AS
SELECT
  b.asset_id,
  b.dt,
  b.open * coalesce(f.total_return_factor, 1.0) AS open,
  b.high * coalesce(f.total_return_factor, 1.0) AS high,
  b.low * coalesce(f.total_return_factor, 1.0) AS low,
  b.close * coalesce(f.total_return_factor, 1.0) AS close,
  b.volume,
  b.amount,
  b.source,
  'total_return'::text AS adjustment,
  b.available_at,
  greatest(
    b.available_at,
    coalesce(f.available_at, b.available_at)
  ) AS factor_available_at,
  CASE
    WHEN b.quality_status IN ('conflict','incomplete','rejected') THEN b.quality_status
    WHEN f.quality_status IN ('conflict','incomplete') THEN f.quality_status
    ELSE b.quality_status
  END AS quality_status,
  jsonb_build_object(
    'bar_source_version', b.source_version,
    'factor_source_version', coalesce(f.source_version, 'identity:no_action'),
    'bar_quality', b.quality,
    'factor_provenance', coalesce(f.provenance, '{}'::jsonb)
  ) AS provenance
FROM market.canonical_bars_daily_v2 b
LEFT JOIN market.adjustment_factors_daily f
  ON f.asset_id=b.asset_id AND f.dt=b.dt;

CREATE TABLE IF NOT EXISTS market.asset_lifecycle_versions (
  lifecycle_version_id bigserial PRIMARY KEY,
  asset_id uuid NOT NULL REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  valid_from date NOT NULL,
  valid_to date,
  lifecycle_status text NOT NULL,
  is_tradable boolean NOT NULL,
  is_st boolean NOT NULL DEFAULT false,
  is_suspended boolean NOT NULL DEFAULT false,
  exchange text,
  source_name text NOT NULL,
  source_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  available_at timestamptz NOT NULL,
  quality_status text NOT NULL DEFAULT 'unverified',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT asset_lifecycle_dates_chk CHECK (valid_to IS NULL OR valid_to >= valid_from),
  CONSTRAINT asset_lifecycle_status_chk
    CHECK (lifecycle_status IN ('pre_listed','active','suspended','delisted','unknown')),
  CONSTRAINT asset_lifecycle_quality_chk
    CHECK (quality_status IN ('verified','unverified','conflict'))
);

CREATE INDEX IF NOT EXISTS asset_lifecycle_lookup_idx
  ON market.asset_lifecycle_versions (asset_id, valid_from DESC, valid_to, available_at DESC);
CREATE INDEX IF NOT EXISTS asset_lifecycle_tradable_idx
  ON market.asset_lifecycle_versions (valid_from, valid_to, asset_id)
  WHERE is_tradable;

CREATE TABLE IF NOT EXISTS company.filings (
  filing_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id uuid REFERENCES market.assets(asset_id) ON DELETE SET NULL,
  stock_code text,
  filing_type text NOT NULL,
  title text NOT NULL,
  period_end date,
  announced_at timestamptz NOT NULL,
  available_at timestamptz NOT NULL,
  source_name text NOT NULL,
  source_external_id text NOT NULL,
  source_url text,
  document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  revision_no integer NOT NULL DEFAULT 1,
  quality_status text NOT NULL DEFAULT 'unverified',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT filings_revision_chk CHECK (revision_no > 0),
  CONSTRAINT filings_quality_chk
    CHECK (quality_status IN ('verified','unverified','conflict','rejected')),
  CONSTRAINT filings_source_unique UNIQUE (source_name, source_external_id, revision_no)
);

CREATE INDEX IF NOT EXISTS filings_asset_period_idx
  ON company.filings (asset_id, period_end DESC, available_at DESC);
CREATE INDEX IF NOT EXISTS filings_available_idx
  ON company.filings (available_at DESC, filing_type, asset_id);

CREATE TABLE IF NOT EXISTS company.financial_facts (
  financial_fact_id bigserial PRIMARY KEY,
  filing_id uuid REFERENCES company.filings(filing_id) ON DELETE CASCADE,
  asset_id uuid REFERENCES market.assets(asset_id) ON DELETE CASCADE,
  stock_code text,
  metric_code text NOT NULL,
  metric_name text NOT NULL,
  fact_kind text NOT NULL,
  statement_scope text NOT NULL DEFAULT 'consolidated',
  period_type text NOT NULL,
  period_start date,
  period_end date NOT NULL,
  value numeric,
  yoy numeric,
  unit text NOT NULL,
  currency text,
  announced_at timestamptz,
  available_at timestamptz NOT NULL,
  revision_no integer NOT NULL DEFAULT 1,
  source_name text NOT NULL,
  source_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  source_text text,
  confidence double precision,
  quality_status text NOT NULL DEFAULT 'unverified',
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT financial_facts_kind_chk
    CHECK (fact_kind IN ('actual','forecast','consensus','report_claim')),
  CONSTRAINT financial_facts_scope_chk
    CHECK (statement_scope IN ('consolidated','parent','unknown')),
  CONSTRAINT financial_facts_period_chk
    CHECK (period_type IN ('quarter','year_to_date','annual','point_in_time','unknown')),
  CONSTRAINT financial_facts_quality_chk
    CHECK (quality_status IN ('verified','unverified','conflict','rejected')),
  CONSTRAINT financial_facts_confidence_chk
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
  CONSTRAINT financial_facts_unique
    UNIQUE NULLS NOT DISTINCT (
      filing_id, asset_id, metric_code, fact_kind, statement_scope,
      period_end, revision_no, source_name
    )
);

CREATE INDEX IF NOT EXISTS financial_facts_asset_metric_idx
  ON company.financial_facts
  (asset_id, fact_kind, metric_code, period_end DESC, available_at DESC);
CREATE INDEX IF NOT EXISTS financial_facts_period_idx
  ON company.financial_facts
  (period_end DESC, fact_kind, metric_code, quality_status, asset_id);
CREATE INDEX IF NOT EXISTS financial_facts_document_idx
  ON company.financial_facts (source_document_id)
  WHERE source_document_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS corpus.document_quality (
  document_id uuid PRIMARY KEY REFERENCES corpus.documents(id) ON DELETE CASCADE,
  quality_status text NOT NULL,
  quality_score double precision NOT NULL,
  title_quality_score double precision,
  body_quality_score double precision,
  template_similarity double precision,
  body_chars integer NOT NULL DEFAULT 0,
  duplicate_of uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  parser_version text,
  content_version bigint NOT NULL DEFAULT 1,
  checks jsonb NOT NULL DEFAULT '{}'::jsonb,
  checked_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT document_quality_status_chk
    CHECK (quality_status IN ('accepted','short','duplicate','template_noise','parse_failed','quarantined')),
  CONSTRAINT document_quality_score_chk
    CHECK (quality_score >= 0 AND quality_score <= 1)
);

CREATE INDEX IF NOT EXISTS document_quality_status_idx
  ON corpus.document_quality (quality_status, checked_at DESC, document_id);
CREATE INDEX IF NOT EXISTS document_quality_recheck_idx
  ON corpus.document_quality (checked_at, document_id)
  WHERE quality_status <> 'accepted';

CREATE TABLE IF NOT EXISTS corpus.document_quarantine (
  quarantine_id bigserial PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES corpus.documents(id) ON DELETE CASCADE,
  reason_code text NOT NULL,
  reason_detail text,
  source_name text,
  source_url text,
  quarantined_at timestamptz NOT NULL DEFAULT now(),
  released_at timestamptz,
  released_by text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT document_quarantine_active_unique
    UNIQUE NULLS NOT DISTINCT (document_id, reason_code, released_at)
);

CREATE INDEX IF NOT EXISTS document_quarantine_active_idx
  ON corpus.document_quarantine (quarantined_at DESC, document_id)
  WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS ops.pipeline_runs_v3 (
  run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pipeline_name text NOT NULL,
  stage_name text NOT NULL,
  scheduled_for timestamptz,
  expected_date date,
  status text NOT NULL DEFAULT 'pending',
  attempt integer NOT NULL DEFAULT 1,
  started_at timestamptz,
  completed_at timestamptz,
  heartbeat_at timestamptz,
  input_watermarks jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_watermarks jsonb NOT NULL DEFAULT '{}'::jsonb,
  quality_status text NOT NULL DEFAULT 'unknown',
  exit_code integer,
  log_path text,
  error_class text,
  error_message text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  CONSTRAINT pipeline_runs_v3_status_chk
    CHECK (status IN ('pending','running','succeeded','failed','skipped','blocked')),
  CONSTRAINT pipeline_runs_v3_quality_chk
    CHECK (quality_status IN ('unknown','pass','warn','fail','degraded')),
  CONSTRAINT pipeline_runs_v3_attempt_chk CHECK (attempt > 0)
);

CREATE INDEX IF NOT EXISTS pipeline_runs_v3_stage_idx
  ON ops.pipeline_runs_v3 (pipeline_name, stage_name, expected_date DESC, started_at DESC);
CREATE INDEX IF NOT EXISTS pipeline_runs_v3_running_idx
  ON ops.pipeline_runs_v3 (heartbeat_at, run_id)
  WHERE status = 'running';

CREATE TABLE IF NOT EXISTS ops.dataset_watermarks (
  dataset_key text NOT NULL,
  partition_key text NOT NULL DEFAULT 'default',
  expected_date date,
  data_through timestamptz,
  row_count bigint,
  eligible_count bigint,
  coverage_pct double precision,
  quality_status text NOT NULL DEFAULT 'unknown',
  schema_version text NOT NULL,
  source_version text,
  run_id uuid REFERENCES ops.pipeline_runs_v3(run_id) ON DELETE SET NULL,
  completed_at timestamptz NOT NULL DEFAULT now(),
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (dataset_key, partition_key),
  CONSTRAINT dataset_watermarks_quality_chk
    CHECK (quality_status IN ('unknown','pass','warn','fail','degraded')),
  CONSTRAINT dataset_watermarks_coverage_chk
    CHECK (coverage_pct IS NULL OR (coverage_pct >= 0 AND coverage_pct <= 100))
);

CREATE INDEX IF NOT EXISTS dataset_watermarks_status_idx
  ON ops.dataset_watermarks (quality_status, completed_at DESC, dataset_key);

CREATE TABLE IF NOT EXISTS ops.quality_results_v3 (
  quality_result_id bigserial PRIMARY KEY,
  run_id uuid REFERENCES ops.pipeline_runs_v3(run_id) ON DELETE CASCADE,
  dataset_key text NOT NULL,
  partition_key text NOT NULL DEFAULT 'default',
  check_name text NOT NULL,
  severity text NOT NULL,
  status text NOT NULL,
  observed_value double precision,
  threshold_value double precision,
  affected_rows bigint,
  sample jsonb NOT NULL DEFAULT '[]'::jsonb,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  checked_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT quality_results_v3_severity_chk CHECK (severity IN ('info','warning','error')),
  CONSTRAINT quality_results_v3_status_chk CHECK (status IN ('pass','warn','fail'))
);

CREATE INDEX IF NOT EXISTS quality_results_v3_fail_idx
  ON ops.quality_results_v3 (checked_at DESC, dataset_key, check_name)
  WHERE status = 'fail';
CREATE INDEX IF NOT EXISTS quality_results_v3_run_idx
  ON ops.quality_results_v3 (run_id, dataset_key, check_name);

CREATE TABLE IF NOT EXISTS agent.message_outbox (
  message_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  channel text NOT NULL,
  target_type text NOT NULL,
  target_id text NOT NULL,
  message_kind text NOT NULL DEFAULT 'chat',
  dedup_key text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL DEFAULT 'pending',
  priority integer NOT NULL DEFAULT 100,
  available_at timestamptz NOT NULL DEFAULT now(),
  claimed_at timestamptz,
  sent_at timestamptz,
  attempt_count integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 6,
  last_error_class text,
  last_error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT message_outbox_channel_chk CHECK (channel IN ('qq','wechat')),
  CONSTRAINT message_outbox_target_chk CHECK (target_type IN ('group','user')),
  CONSTRAINT message_outbox_kind_chk CHECK (message_kind IN ('chat','daily_report','daily_brief','alert','file')),
  CONSTRAINT message_outbox_status_chk CHECK (status IN ('pending','sending','sent','failed','dead_letter','cancelled')),
  CONSTRAINT message_outbox_attempt_chk CHECK (attempt_count >= 0 AND max_attempts > 0),
  CONSTRAINT message_outbox_dedup_unique UNIQUE (channel, target_type, target_id, dedup_key)
);

CREATE INDEX IF NOT EXISTS message_outbox_pending_idx
  ON agent.message_outbox (priority, available_at, created_at, message_id)
  WHERE status IN ('pending','failed');
CREATE INDEX IF NOT EXISTS message_outbox_status_idx
  ON agent.message_outbox (status, updated_at DESC, channel);

CREATE TABLE IF NOT EXISTS agent.access_policies (
  channel text NOT NULL,
  subject_type text NOT NULL,
  subject_id text NOT NULL,
  policy_status text NOT NULL DEFAULT 'allowed',
  daily_budget integer NOT NULL DEFAULT 10,
  min_interval_seconds integer NOT NULL DEFAULT 0,
  is_admin boolean NOT NULL DEFAULT false,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (channel, subject_type, subject_id),
  CONSTRAINT access_policies_channel_chk CHECK (channel IN ('qq','wechat')),
  CONSTRAINT access_policies_subject_chk CHECK (subject_type IN ('user','group')),
  CONSTRAINT access_policies_status_chk CHECK (policy_status IN ('allowed','blocked')),
  CONSTRAINT access_policies_budget_chk CHECK (daily_budget >= 0 AND min_interval_seconds >= 0)
);

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

CREATE TABLE IF NOT EXISTS agent.usage_daily (
  usage_date date NOT NULL,
  channel text NOT NULL,
  subject_type text NOT NULL,
  subject_id text NOT NULL,
  request_count integer NOT NULL DEFAULT 0,
  tool_call_count integer NOT NULL DEFAULT 0,
  model_call_count integer NOT NULL DEFAULT 0,
  input_tokens bigint NOT NULL DEFAULT 0,
  output_tokens bigint NOT NULL DEFAULT 0,
  last_request_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (usage_date, channel, subject_type, subject_id),
  CONSTRAINT usage_daily_channel_chk CHECK (channel IN ('qq','wechat')),
  CONSTRAINT usage_daily_subject_chk CHECK (subject_type IN ('user','group')),
  CONSTRAINT usage_daily_nonnegative_chk CHECK (
    request_count >= 0 AND tool_call_count >= 0 AND model_call_count >= 0
    AND input_tokens >= 0 AND output_tokens >= 0
  )
);

CREATE TABLE IF NOT EXISTS agent.turn_evidence (
  turn_id uuid NOT NULL,
  evidence_id text NOT NULL,
  tool_name text NOT NULL,
  claim_type text NOT NULL,
  entity_id text,
  metric_id text,
  value_json jsonb,
  unit text,
  period_end date,
  as_of_date date,
  available_at timestamptz,
  source_id text,
  source_document_id uuid REFERENCES corpus.documents(id) ON DELETE SET NULL,
  quality_status text NOT NULL DEFAULT 'unverified',
  evidence_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (turn_id, evidence_id),
  CONSTRAINT turn_evidence_claim_type_chk
    CHECK (claim_type IN ('actual','forecast','consensus','market','model_signal','document_claim','graph_association','calculation','data_quality')),
  CONSTRAINT turn_evidence_quality_chk
    CHECK (quality_status IN ('verified','unverified','conflict','stale','incomplete','rejected'))
);

CREATE INDEX IF NOT EXISTS turn_evidence_created_idx
  ON agent.turn_evidence (created_at DESC, turn_id);

CREATE TABLE IF NOT EXISTS agent.answer_audits (
  answer_audit_id bigserial PRIMARY KEY,
  turn_id uuid NOT NULL,
  channel text,
  draft_hash text,
  final_hash text,
  validation_status text NOT NULL,
  repair_attempted boolean NOT NULL DEFAULT false,
  violations jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence_count integer NOT NULL DEFAULT 0,
  generated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT answer_audits_status_chk
    CHECK (validation_status IN ('passed','repaired','degraded','failed'))
);

CREATE INDEX IF NOT EXISTS answer_audits_turn_idx
  ON agent.answer_audits (turn_id, generated_at DESC);

CREATE TABLE IF NOT EXISTS agent.model_calls (
  model_call_id bigserial PRIMARY KEY,
  turn_id uuid,
  provider text NOT NULL,
  model text NOT NULL,
  purpose text NOT NULL,
  prompt_hash text,
  tool_schema_hash text,
  response_schema_hash text,
  duration_ms double precision,
  input_tokens bigint,
  output_tokens bigint,
  status text NOT NULL,
  error_class text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT model_calls_status_chk CHECK (status IN ('ok','error','timeout','cancelled'))
);

CREATE INDEX IF NOT EXISTS model_calls_turn_idx
  ON agent.model_calls (turn_id, created_at DESC);
CREATE INDEX IF NOT EXISTS model_calls_health_idx
  ON agent.model_calls (provider, model, created_at DESC, status);

CREATE TABLE IF NOT EXISTS agent.report_runs (
  report_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  report_date date NOT NULL,
  report_type text NOT NULL DEFAULT 'ai_hardware_daily',
  status text NOT NULL DEFAULT 'building',
  decision_cutoff timestamptz NOT NULL,
  market_date date,
  data_snapshot jsonb NOT NULL,
  model_provider text,
  model_name text,
  validation jsonb NOT NULL DEFAULT '{}'::jsonb,
  output_path text,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CONSTRAINT report_runs_status_chk
    CHECK (status IN ('building','published','degraded','quarantined','failed')),
  CONSTRAINT report_runs_unique UNIQUE (report_type, report_date, decision_cutoff)
);

CREATE INDEX IF NOT EXISTS report_runs_date_idx
  ON agent.report_runs (report_type, report_date DESC, status);

CREATE TABLE IF NOT EXISTS agent.report_claims (
  report_run_id uuid NOT NULL REFERENCES agent.report_runs(report_run_id) ON DELETE CASCADE,
  claim_id text NOT NULL,
  section_name text NOT NULL,
  claim_type text NOT NULL,
  claim_text text NOT NULL,
  evidence_ids text[] NOT NULL DEFAULT '{}'::text[],
  numeric_value numeric,
  unit text,
  as_of_date date,
  validation_status text NOT NULL DEFAULT 'pending',
  validation_error text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (report_run_id, claim_id),
  CONSTRAINT report_claims_type_chk
    CHECK (claim_type IN ('actual','forecast','market','model_signal','document_claim','graph_association','calculation','data_quality','interpretation')),
  CONSTRAINT report_claims_status_chk
    CHECK (validation_status IN ('pending','verified','rejected','unverifiable'))
);

CREATE TABLE IF NOT EXISTS analytics.dataset_coverage_latest (
  dataset_key text NOT NULL,
  partition_key text NOT NULL DEFAULT 'default',
  data_through timestamptz,
  row_count bigint,
  entity_count bigint,
  eligible_count bigint,
  coverage_pct double precision,
  quality_status text NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  refreshed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (dataset_key, partition_key),
  CONSTRAINT dataset_coverage_latest_quality_chk
    CHECK (quality_status IN ('unknown','pass','warn','fail','degraded'))
);

CREATE INDEX IF NOT EXISTS dataset_coverage_latest_status_idx
  ON analytics.dataset_coverage_latest (quality_status, refreshed_at DESC, dataset_key);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb') THEN
    PERFORM public.create_hypertable(
      'market.canonical_bars_daily_v2', 'dt',
      chunk_time_interval => INTERVAL '90 days',
      migrate_data => TRUE, if_not_exists => TRUE
    );
    EXECUTE 'ALTER TABLE market.canonical_bars_daily_v2 SET (
      timescaledb.compress,
      timescaledb.compress_segmentby = ''asset_id'',
      timescaledb.compress_orderby = ''dt DESC''
    )';
  END IF;
END;
$$;

DO $$
BEGIN
  IF to_regclass('factor.definitions') IS NOT NULL THEN
    ALTER TABLE factor.definitions DROP CONSTRAINT IF EXISTS factor_lifecycle_chk;
    ALTER TABLE factor.definitions ADD CONSTRAINT factor_lifecycle_chk
      CHECK (lifecycle_status IN ('candidate','shadow','production','degraded','retired'));
  END IF;
  IF to_regclass('factor.factor_health_daily') IS NOT NULL THEN
    ALTER TABLE factor.factor_health_daily DROP CONSTRAINT IF EXISTS factor_health_lifecycle_chk;
    ALTER TABLE factor.factor_health_daily ADD CONSTRAINT factor_health_lifecycle_chk
      CHECK (lifecycle_status IN ('candidate','shadow','production','degraded','retired'));
  END IF;
END;
$$;

INSERT INTO ops.schema_migrations (migration_id, description, checksum, metadata)
VALUES (
  '20260810_finance_agent_v3',
  'Finance Agent V3 dual-track data, quality, evidence, report, usage and outbox schema',
  'manual:v1',
  jsonb_build_object('dual_track', true, 'destructive', false)
)
ON CONFLICT (migration_id) DO UPDATE
SET description=EXCLUDED.description,
    checksum=EXCLUDED.checksum,
    metadata=ops.schema_migrations.metadata || EXCLUDED.metadata;

GRANT USAGE ON SCHEMA ops, agent, market, company, corpus, analytics TO finance_app, finance_ai_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ops TO finance_app;
GRANT SELECT ON ALL TABLES IN SCHEMA ops TO finance_ai_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON
  market.trading_calendar, market.corporate_actions, market.adjustment_factors_daily,
  market.canonical_bars_daily_v2, market.asset_lifecycle_versions,
  company.filings, company.financial_facts,
  corpus.document_quality, corpus.document_quarantine,
  agent.message_outbox, agent.access_policies, agent.access_defaults, agent.usage_daily,
  agent.turn_evidence, agent.answer_audits, agent.model_calls,
  agent.report_runs, agent.report_claims,
  analytics.dataset_coverage_latest
TO finance_app;
GRANT SELECT ON
  market.trading_calendar, market.corporate_actions, market.adjustment_factors_daily,
  market.canonical_bars_daily_v2, market.asset_lifecycle_versions,
  company.filings, company.financial_facts,
  corpus.document_quality, corpus.document_quarantine,
  agent.message_outbox, agent.access_policies, agent.access_defaults, agent.usage_daily,
  agent.turn_evidence, agent.answer_audits, agent.model_calls,
  agent.report_runs, agent.report_claims,
  analytics.dataset_coverage_latest
TO finance_ai_reader;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ops, agent, market, company, corpus, analytics TO finance_app;

COMMIT;
