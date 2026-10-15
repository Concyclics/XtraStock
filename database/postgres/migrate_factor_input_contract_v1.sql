BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE analytics.asset_technical_daily
 ADD COLUMN IF NOT EXISTS input_quality jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE factor.feature_daily
 ADD COLUMN IF NOT EXISTS input_contract_version text NOT NULL DEFAULT 'legacy',
 ADD COLUMN IF NOT EXISTS decision_cutoff timestamptz,
 ADD COLUMN IF NOT EXISTS knowledge_mode text NOT NULL DEFAULT 'legacy',
 ADD COLUMN IF NOT EXISTS input_quality jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE factor.forward_returns
 ADD COLUMN IF NOT EXISTS input_contract_version text NOT NULL DEFAULT 'legacy',
 ADD COLUMN IF NOT EXISTS label_available_at_1d timestamptz,
 ADD COLUMN IF NOT EXISTS label_available_at_5d timestamptz,
 ADD COLUMN IF NOT EXISTS label_available_at_20d timestamptz,
 ADD COLUMN IF NOT EXISTS label_available_at_60d timestamptz,
 ADD COLUMN IF NOT EXISTS intraday_return_next_session double precision,
 ADD COLUMN IF NOT EXISTS input_quality jsonb NOT NULL DEFAULT '{}'::jsonb;
COMMENT ON COLUMN factor.forward_returns.return_1d IS
 'For pit-input-v1 stock labels this is NULL: next-session open to same-session close is not a T+1-executable stock holding return. See intraday_return_next_session for diagnostic price move.';
COMMENT ON COLUMN factor.forward_returns.label_available_at IS
 'Legacy compatibility only: pit-input-v1 uses the latest availability across all four horizons only when all four outcomes are mature; use label_available_at_<h>d for horizon-specific research.';
COMMENT ON COLUMN factor.feature_daily.knowledge_mode IS
 'public is reconstructed public knowledge, not proof of historically recorded live information; system requires each source to satisfy system-time visibility.';
COMMIT;
