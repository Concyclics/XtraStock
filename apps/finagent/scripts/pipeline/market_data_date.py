"""Select an audited, complete market date, never max(partially loaded prices)."""
from __future__ import annotations
import datetime as dt

def complete_market_date(conn, requested, cutoff=None):
    rows = conn.execute("""WITH latest AS (
        SELECT DISTINCT ON(expected_date) expected_date,status,quality_status,output_watermarks,completed_at
        FROM ops.pipeline_runs_v3 WHERE stage_name='canonical_daily_v2'
          AND expected_date<=%s AND (%s::timestamptz IS NULL OR completed_at<=%s)
          AND status IN ('succeeded','failed')
        ORDER BY expected_date,completed_at DESC
    ) SELECT * FROM latest WHERE status='succeeded' AND quality_status IN ('pass','warn')
        AND (output_watermarks->>'coverage_pct')::numeric>=99
        ORDER BY expected_date DESC LIMIT 1""", (requested,cutoff,cutoff)).fetchone()
    if not rows:
        raise RuntimeError("no audited market date with >=99% eligible coverage")
    age = conn.execute("""SELECT count(DISTINCT trade_date) n FROM market.trading_calendar
        WHERE exchange='SSE' AND session_status='open' AND trade_date>%s AND trade_date<=%s
          AND coalesce(close_at,(trade_date::timestamp+time '15:05') AT TIME ZONE 'Asia/Shanghai')<=coalesce(%s,now())""",
        (rows["expected_date"],requested,cutoff)).fetchone()["n"]
    return rows["expected_date"], {"requested_date": str(requested),"market_date": str(rows["expected_date"]),
        "stale_sessions": age,"quality_status": rows["quality_status"],
        "coverage_pct": float(rows["output_watermarks"]["coverage_pct"])}
