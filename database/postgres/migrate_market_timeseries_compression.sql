ALTER TABLE market.bars_daily SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'asset_id,source',
  timescaledb.compress_orderby = 'ts DESC'
);

ALTER TABLE market.bars_1m SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'asset_id,source',
  timescaledb.compress_orderby = 'ts DESC'
);

ALTER TABLE market.trade_ticks SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'asset_id,source',
  timescaledb.compress_orderby = 'ts DESC'
);

ALTER TABLE market.quote_snapshots SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'asset_id,source',
  timescaledb.compress_orderby = 'ts DESC'
);

ALTER TABLE market.order_book_snapshots SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'asset_id,source',
  timescaledb.compress_orderby = 'ts DESC'
);

SELECT public.add_compression_policy('market.bars_daily'::regclass, INTERVAL '30 days', if_not_exists => TRUE);
SELECT public.add_compression_policy('market.bars_1m'::regclass, INTERVAL '7 days', if_not_exists => TRUE);
SELECT public.add_compression_policy('market.trade_ticks'::regclass, INTERVAL '3 days', if_not_exists => TRUE);

SELECT public.compress_chunk(chunk, if_not_compressed => TRUE)
FROM public.show_chunks('market.bars_daily'::regclass, older_than => INTERVAL '30 days') AS chunk;

SELECT public.compress_chunk(chunk, if_not_compressed => TRUE)
FROM public.show_chunks('market.bars_1m'::regclass, older_than => INTERVAL '7 days') AS chunk;

SELECT public.compress_chunk(chunk, if_not_compressed => TRUE)
FROM public.show_chunks('market.trade_ticks'::regclass, older_than => INTERVAL '3 days') AS chunk;
