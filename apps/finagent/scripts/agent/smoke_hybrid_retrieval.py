#!/usr/bin/env python3
"""Verify that live document retrieval uses both lexical and semantic channels."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "MMClaw"))

from mmclaw.tools import FinanceDBTool  # noqa: E402


def main() -> int:
    raw = FinanceDBTool.document_retrieve_v3(
        query="人工智能服务器需求增长",
        days=365,
        intent="evidence",
        backend="hybrid",
        limit=5,
    )
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        print(json.dumps({'passed':False,'error':'non_json_tool_response','detail':str(raw)[:1500]},ensure_ascii=False))
        return 1
    meta = payload.get("meta") or {}
    checks = {
        "rows": bool(payload.get("rows")),
        "hybrid_used": meta.get("hybrid_used") is True,
        "semantic_used": meta.get("semantic_used") is True,
        "lexical_hits": int(meta.get("lexical_hits") or 0) > 0,
        "semantic_hits": int(meta.get("semantic_hits") or 0) > 0,
        "coverage_reported": isinstance(meta.get("semantic_coverage_pct"),(int,float)),
    }
    print(json.dumps({"passed": all(checks.values()), "checks": checks, "meta": meta}, ensure_ascii=False, indent=2, default=str))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
