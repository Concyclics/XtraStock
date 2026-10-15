#!/usr/bin/env python3
"""Live database smoke checks for parameterized finance analysis tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "MMClaw"))

from mmclaw.tools import FinanceDBTool  # noqa: E402


CASES = [
    ("fundamental_analysis", lambda: FinanceDBTool.fundamental_analysis(stock_code="000001", periods=2)),
    ("peer_compare", lambda: FinanceDBTool.peer_compare(stock_code="000001", limit=3)),
    ("relative_valuation", lambda: FinanceDBTool.relative_valuation(stock_code="000001", lookback_days=365)),
    (
        "capital_flow_analysis",
        lambda: FinanceDBTool.capital_flow_analysis(
            stock_code="000610", days=60, categories=["block_trade", "dragon_tiger"], limit=3
        ),
    ),
    ("macro_context", lambda: FinanceDBTool.macro_context(indicators=["CPI", "PMI"], periods=2)),
]


def main() -> int:
    results = []
    for name, call in CASES:
        raw = call()
        error = raw.startswith("FinanceDB error:") or raw.startswith("Error:")
        try:
            payload = json.loads(raw) if not error else {}
        except json.JSONDecodeError:
            payload = {}
            error = True
        meta = payload.get("meta") or {}
        rows = payload.get("rows") or []
        passed = not error and int(meta.get("row_count") or 0) > 0 and bool(rows)
        results.append({
            "tool": f"finance_{name}",
            "passed": passed,
            "row_count": meta.get("row_count"),
            "coverage_status": meta.get("coverage_status"),
            "error_preview": raw[:240] if error else None,
        })
    report = {"passed": sum(row["passed"] for row in results), "total": len(results), "cases": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
