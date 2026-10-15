-- Typed source filtering otherwise suffers from independent JSON selectivity
-- estimates, leading to billion-comparison nested loops in graph refreshes.
CREATE STATISTICS IF NOT EXISTS research.forecast_semantics_stats (mcv,dependencies)
 ON (metadata->>'semantic_contract'),(metadata->>'year_verified'),
    (metadata->>'eps_unit'),(metadata->>'net_profit_unit'),
    (metadata->>'net_profit_scope'),(metadata->>'revenue_unit')
 FROM research.report_forecasts;
ANALYZE research.report_forecasts;
