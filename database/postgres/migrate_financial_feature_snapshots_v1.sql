BEGIN;
SET LOCAL lock_timeout='3s';
CREATE TABLE IF NOT EXISTS factor.financial_feature_snapshots (
 snapshot_id uuid NOT NULL,
 asset_id uuid NOT NULL REFERENCES market.assets(asset_id),
 as_of_date date NOT NULL,
 decision_cutoff timestamptz NOT NULL,
 knowledge_mode text NOT NULL CHECK(knowledge_mode IN ('system','public')),
 feature_name text NOT NULL CHECK(feature_name IN ('reported_net_margin','reported_liabilities_assets','reported_operating_cash_margin')),
 feature_value double precision NOT NULL,
 period_start date,
 period_end date NOT NULL,
 period_type text NOT NULL,
 statement_scope text NOT NULL,
 available_at timestamptz NOT NULL,
 source_fact_ids bigint[] NOT NULL,
 source_document_ids uuid[] NOT NULL,
 input_contract_version text NOT NULL,
 quality_status text NOT NULL DEFAULT 'verified_inputs_research_only',
 metadata jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(snapshot_id,asset_id,feature_name)
);
CREATE INDEX IF NOT EXISTS financial_feature_snapshot_date_idx
 ON factor.financial_feature_snapshots(as_of_date DESC,feature_name,asset_id);
COMMENT ON TABLE factor.financial_feature_snapshots IS
 'Append-only research ratio snapshots from matching verified consolidated facts. No TTM imputation and no production-factor promotion; source IDs, values and input hashes are retained.';
GRANT SELECT,INSERT ON factor.financial_feature_snapshots TO finance_app;
GRANT SELECT ON factor.financial_feature_snapshots TO finance_ai_reader;
COMMIT;
