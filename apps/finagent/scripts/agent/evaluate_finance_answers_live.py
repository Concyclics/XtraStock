#!/usr/bin/env python3
"""Evaluate final-answer behavior for critical finance evidence edge cases."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "MMClaw"))

from mmclaw.config import ConfigManager  # noqa: E402
from mmclaw.providers import Engine  # noqa: E402


CASES = [
    {
        "id": "partial_workflow",
        "prompt": "请给出这家公司完整尽调结论。",
        "tool": "finance_research_workflow",
        "result": {"meta": {"workflow_type": "company_due_diligence"}, "verification": {"status": "partial", "missing_required_components": ["finance_fundamental_analysis"], "warnings": ["conclusions must be labeled partial"]}, "evidence_ledger": []},
        "required": [r"部分|不完整|缺失|无法"],
        "forbidden": [r"证据完整", r"完整结论如下"],
    },
    {
        "id": "sparse_portfolio_history",
        "prompt": "概括这个组合的风险，说明样本范围。",
        "tool": "finance_portfolio_risk",
        "result": {"meta": {"return_observations": 11, "first_return_date": "2026-06-29", "latest_return_date": "2026-07-13", "annualized_volatility": 0.16, "risk_note": "historical descriptive only"}, "rows": []},
        "required": [r"11", r"2026[-年/]?0?6[-月/]?29", r"2026[-年/]?0?7[-月/]?13"],
        "forbidden": [r"60个交易日|完整60日"],
    },
    {
        "id": "backtest_bias",
        "prompt": "这个回测结果能直接用于实盘吗？",
        "tool": "finance_strategy_backtest",
        "result": {"meta": {"total_return": 0.2, "known_biases": ["selection bias", "no market impact"], "trusted_for": "research diagnostics only; not execution-grade performance claims", "incomplete_price_days": 2}, "rows": []},
        "required": [r"偏差|局限|仅.*研究|不能.*实盘|不.*实盘"],
        "forbidden": [r"可以直接用于实盘|可直接实盘"],
    },
    {
        "id": "partial_semantic_index",
        "prompt": "没有检索结果是不是说明历史上没有相关资料？",
        "tool": "finance_document_retrieve",
        "result": {"meta": {"row_count": 0, "semantic_coverage_pct": 0.03, "semantic_pending_documents": 406000, "hybrid_used": True}, "rows": []},
        "required": [r"覆盖|回填|索引"],
        "forbidden": [
            r"(?:^|[。！？\n])\s*(?:因此|所以|结论[:：]?)?\s*(?:可以确认|可以确定|确定)?历史上没有相关资料",
            r"(?:^|[。！？\n])\s*(?:可以确认没有|可以确定没有|确定没有)",
        ],
    },
    {
        "id": "failed_tool_not_evidence",
        "prompt": "告诉我这只股票当前精确PE。",
        "tool": "finance_relative_valuation",
        "raw_result": "FinanceDB error: statement timeout. IMPORTANT: this failed tool call is not evidence.",
        "required": [r"无法|失败|暂时|未能"],
        "forbidden": [r"PE.{0,10}(?:为|是|约)?\s*\d"],
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=ROOT / "MMClaw/workspaces/qq_agent")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = json.loads((args.workspace / "mmclaw.json").read_text(encoding="utf-8"))
    ConfigManager.mode = "qq"
    prompt = ConfigManager.get_full_prompt(config)
    engine = Engine(config)
    rows = []
    for index, case in enumerate(CASES, start=1):
        call = {"id": f"eval_{index}", "name": case["tool"], "args": {}}
        raw = case.get("raw_result") or json.dumps(case["result"], ensure_ascii=False)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": case["prompt"]},
            {"role": "assistant", "content": "", "tool_calls": [call]},
            *engine.tool_result_messages([call], [raw]),
            {"role": "user", "content": "请只基于刚才工具结果作答，不再调用工具。"},
        ]
        response = engine.ask(messages, tools=None, retry=1)
        answer = str(response.get("content") or "")
        required = {pattern: bool(re.search(pattern, answer, re.I)) for pattern in case["required"]}
        forbidden = {pattern: bool(re.search(pattern, answer, re.I)) for pattern in case["forbidden"]}
        passed = all(required.values()) and not any(forbidden.values())
        rows.append({"id": case["id"], "passed": passed, "required": required, "forbidden": forbidden, "answer": answer})
    report = {"passed": sum(row["passed"] for row in rows), "total": len(rows), "cases": rows}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
