-- Apply outside a transaction so readers/writers remain available.
CREATE INDEX CONCURRENTLY IF NOT EXISTS market_entities_stock_code_idx
  ON kg.market_entities ((metadata->>'stock_code'));
ANALYZE kg.market_entities;
