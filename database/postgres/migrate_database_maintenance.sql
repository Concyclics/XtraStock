BEGIN;

-- The daily technical and crawler state tables are update-heavy. Lowering their
-- per-table thresholds keeps dead tuples from accumulating between maintenance
-- cycles without imposing aggressive settings on append-heavy source tables.
ALTER TABLE analytics.asset_technical_daily SET (
  autovacuum_vacuum_scale_factor = 0.02,
  autovacuum_vacuum_threshold = 50000,
  autovacuum_analyze_scale_factor = 0.01,
  autovacuum_analyze_threshold = 25000
);

ALTER TABLE corpus.document_search_index SET (
  autovacuum_vacuum_scale_factor = 0.03,
  autovacuum_vacuum_threshold = 10000,
  autovacuum_analyze_scale_factor = 0.02,
  autovacuum_analyze_threshold = 10000
);

ALTER TABLE market.money_flow_daily SET (
  autovacuum_vacuum_scale_factor = 0.03,
  autovacuum_vacuum_threshold = 10000,
  autovacuum_analyze_scale_factor = 0.02,
  autovacuum_analyze_threshold = 10000
);

ALTER TABLE ops.ocr_queue SET (
  autovacuum_vacuum_scale_factor = 0.05,
  autovacuum_vacuum_threshold = 5000,
  autovacuum_analyze_scale_factor = 0.03,
  autovacuum_analyze_threshold = 5000
);

ALTER TABLE market.crawl_state SET (
  autovacuum_vacuum_scale_factor = 0.05,
  autovacuum_vacuum_threshold = 5000,
  autovacuum_analyze_scale_factor = 0.03,
  autovacuum_analyze_threshold = 5000
);

-- OCR workers always claim pending work ordered by priority and age. A partial
-- index avoids scanning completed OCR rows as the queue history grows.
CREATE INDEX IF NOT EXISTS ocr_queue_pending_claim_idx
  ON ops.ocr_queue (priority, discovered_at, id)
  WHERE status = 'pending';

COMMIT;
