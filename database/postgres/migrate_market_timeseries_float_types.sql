-- Requires enough max_locks_per_transaction for existing Timescale chunks.
-- This migration must be applied before Timescale chunks are compressed.
-- If compression is already enabled and chunks are compressed, do not run this
-- directly; either decompress all chunks first or migrate into new float-typed
-- hypertables and swap them during a maintenance window.
--
-- If the database has already accumulated many chunks, run:
--   ALTER SYSTEM SET max_locks_per_transaction = 512;
-- then restart Postgres before applying this migration.

ALTER TABLE market.bars_daily
  ALTER COLUMN open TYPE double precision USING open::double precision,
  ALTER COLUMN high TYPE double precision USING high::double precision,
  ALTER COLUMN low TYPE double precision USING low::double precision,
  ALTER COLUMN close TYPE double precision USING close::double precision,
  ALTER COLUMN volume TYPE double precision USING volume::double precision,
  ALTER COLUMN amount TYPE double precision USING amount::double precision,
  ALTER COLUMN adj_factor TYPE double precision USING adj_factor::double precision;

ALTER TABLE market.bars_1m
  ALTER COLUMN open TYPE double precision USING open::double precision,
  ALTER COLUMN high TYPE double precision USING high::double precision,
  ALTER COLUMN low TYPE double precision USING low::double precision,
  ALTER COLUMN close TYPE double precision USING close::double precision,
  ALTER COLUMN volume TYPE double precision USING volume::double precision,
  ALTER COLUMN amount TYPE double precision USING amount::double precision;

ALTER TABLE market.trade_ticks
  ALTER COLUMN price TYPE double precision USING price::double precision,
  ALTER COLUMN volume TYPE double precision USING volume::double precision;
