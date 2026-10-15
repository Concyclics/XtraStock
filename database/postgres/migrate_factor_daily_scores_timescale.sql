\echo 'Migrating factor.daily_scores to a compressed Timescale hypertable'

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb') THEN
    PERFORM public.create_hypertable(
      'factor.daily_scores', 'dt',
      chunk_time_interval => INTERVAL '90 days',
      migrate_data => TRUE, if_not_exists => TRUE
    );
    EXECUTE 'ALTER TABLE factor.daily_scores SET (
      timescaledb.compress,
      timescaledb.compress_segmentby = ''factor_id'',
      timescaledb.compress_orderby = ''dt DESC, asset_id''
    )';
    PERFORM public.add_compression_policy(
      'factor.daily_scores', INTERVAL '60 days', if_not_exists => TRUE
    );
  END IF;
END
$$;

ANALYZE factor.daily_scores;
