#!/usr/bin/env python3
"""Track frozen watchlists without hindsight edits to selection records."""
from common import connect

def main():
    with connect(autocommit=False) as conn:
        conn.execute("SET LOCAL statement_timeout='120s'")
        result=conn.execute("""INSERT INTO agent.watchlist_outcomes(snapshot_id,stock_code,horizon_days,exit_date,realized_return,benchmark_return,status)
        SELECT d.snapshot_id,d.stock_code,h.n,
            CASE h.n WHEN 5 THEN f.exit_date_5d WHEN 20 THEN f.exit_date_20d ELSE f.exit_date_60d END,
            CASE WHEN (CASE h.n WHEN 5 THEN f.return_5d WHEN 20 THEN f.return_20d ELSE f.return_60d END) IS NOT NULL
              THEN CASE h.n WHEN 5 THEN f.exit_price_5d WHEN 20 THEN f.exit_price_20d ELSE f.exit_price_60d END/nullif(b.close,0)-1 END,
            NULL,
            CASE WHEN (CASE h.n WHEN 5 THEN f.return_5d WHEN 20 THEN f.return_20d ELSE f.return_60d END) IS NULL OR b.close IS NULL OR b.close=0 THEN 'pending' ELSE 'matured' END
        FROM agent.watchlist_decisions d JOIN analytics.research_snapshot_runs r USING(snapshot_id)
        JOIN analytics.research_snapshot_daily s ON s.snapshot_id=d.snapshot_id AND s.stock_code=d.stock_code
        CROSS JOIN (VALUES(5),(20),(60)) h(n)
        LEFT JOIN factor.forward_returns f ON f.asset_id=s.asset_id AND f.signal_date=r.as_of_date
        LEFT JOIN market.canonical_total_return_daily_v2 b ON b.asset_id=s.asset_id AND b.dt=r.as_of_date
        WHERE d.decision IN ('added','retained') AND r.status='complete'
        ON CONFLICT(snapshot_id,stock_code,horizon_days) DO UPDATE SET
          exit_date=excluded.exit_date,realized_return=excluded.realized_return,status=excluded.status,updated_at=now()""")
        conn.commit();print({'outcomes_tracked':result.rowcount,'return_basis':'total return from reference close; benchmark not yet available is NULL'})
    return 0
if __name__=='__main__':raise SystemExit(main())
