SELECT public.decompress_chunk(chunk, if_compressed => TRUE)
FROM public.show_chunks('market.bars_daily'::regclass) AS chunk;

SELECT public.decompress_chunk(chunk, if_compressed => TRUE)
FROM public.show_chunks('market.bars_1m'::regclass) AS chunk;

SELECT public.decompress_chunk(chunk, if_compressed => TRUE)
FROM public.show_chunks('market.trade_ticks'::regclass) AS chunk;

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

SELECT public.compress_chunk(chunk, if_not_compressed => TRUE)
FROM public.show_chunks('market.bars_daily'::regclass, older_than => INTERVAL '30 days') AS chunk;

SELECT public.compress_chunk(chunk, if_not_compressed => TRUE)
FROM public.show_chunks('market.bars_1m'::regclass, older_than => INTERVAL '7 days') AS chunk;

SELECT public.compress_chunk(chunk, if_not_compressed => TRUE)
FROM public.show_chunks('market.trade_ticks'::regclass, older_than => INTERVAL '3 days') AS chunk;
