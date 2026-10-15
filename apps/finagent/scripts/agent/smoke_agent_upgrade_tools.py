#!/usr/bin/env python3
"""Live database smoke checks for upgraded finance-agent capabilities."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "MMClaw"))

from mmclaw.tools import FinanceDBTool  # noqa: E402


CASES = [
    ("factor_catalog", lambda: FinanceDBTool.factor_catalog(limit=5), lambda p: p["meta"].get("row_count", 0) > 0),
    ("factor_readiness", lambda: FinanceDBTool.factor_readiness(factor_name="low_volatility_20d", limit=3), lambda p: p["meta"].get("row_count", 0) == 1 and "blockers" in p["rows"][0]),
    ("factor_attribution", lambda: FinanceDBTool.factor_attribution(stock_code="000001", as_of_date="2026-07-17", limit=5), lambda p: p["meta"].get("row_count", 0) > 0),
    ("knowledge_graph", lambda: FinanceDBTool.knowledge_graph(stock_code="000001", as_of_date="2026-07-17", limit=5), lambda p: p["meta"].get("row_count", 0) > 0),
    ("watchlist", lambda: FinanceDBTool.watchlist_monitor(stock_codes=["000001", "600519"], days=30), lambda p: p["meta"].get("row_count") == 2),
    ("portfolio_risk", lambda: FinanceDBTool.portfolio_risk(holdings=[{"stock_code": "000001", "weight": 0.6}, {"stock_code": "600519", "weight": 0.4}], as_of_date="2026-07-16", lookback_days=60), lambda p: p["meta"].get("return_observations", 0) > 0 and p["meta"].get("first_return_date")),
    ("event_calendar", lambda: FinanceDBTool.event_calendar(start_date="2026-06-01", end_date="2026-07-16", limit=5), lambda p: "quality_note" in p["meta"]),
    ("backtest", lambda: FinanceDBTool.strategy_backtest(strategy="momentum_20d", start_date="2026-04-01", end_date="2026-07-16", stock_codes=["000001", "600519", "000858", "601318", "600036"], top_n=2, rebalance_days=10), lambda p: p["meta"].get("row_count", 0) > 0 and "known_biases" in p["meta"]),
    ("workflow", lambda: FinanceDBTool.research_workflow(workflow_type="peer_comparison", stock_code="000001", days=180, limit=3), lambda p: p.get("verification", {}).get("status") in {"complete", "partial"} and p.get("evidence_ledger")),
]


def main() -> int:
    rows = []
    for name, call, validate in CASES:
        try:
            raw = call()
            if raw.startswith(("FinanceDB error:", "Error:")):
                raise RuntimeError(raw[:1000])
            payload = json.loads(raw)
            passed = bool(validate(payload))
            error = None
        except Exception as exc:
            payload = {}
            passed = False
            error = f"{type(exc).__name__}: {exc}"[:1200]
        rows.append({"tool": name, "passed": passed, "meta": payload.get("meta"), "error": error})
    report = {"passed": sum(row["passed"] for row in rows), "total": len(rows), "cases": rows}
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
