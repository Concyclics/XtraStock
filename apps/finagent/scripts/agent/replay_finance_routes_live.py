#!/usr/bin/env python3
"""Run a bounded live first-tool routing regression against the chat provider."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "MMClaw"))

from mmclaw.config import ConfigManager  # noqa: E402
from mmclaw.providers import Engine  # noqa: E402
from mmclaw.tool_schemas import get_native_tool_schemas  # noqa: E402


CASES = [
    {
        "id": "fundamental_analysis",
        "prompt": "分析平安银行最近四个报告期的实际营收、归母净利润、EPS和ROE变化，不要用研报预测替代。",
        "expected_tool": "finance_fundamental_analysis",
    },
    {
        "id": "peer_comparison",
        "prompt": "把平安银行与当前同行比较估值、预期增长和近20日走势，并说明同行范围。",
        "expected_tool": "finance_peer_compare",
    },
    {
        "id": "relative_valuation",
        "prompt": "平安银行当前PE和PB处于自身近三年历史什么分位？",
        "expected_tool": "finance_relative_valuation",
    },
    {
        "id": "capital_flow",
        "prompt": "检查平安银行最近30天资金流、龙虎榜和大宗交易情况，并披露各类数据日期。",
        "expected_tool": "finance_capital_flow_analysis",
    },
    {
        "id": "macro_context",
        "prompt": "查询最近六期CPI和制造业PMI，保留原始单位和观测期。",
        "expected_tool": "finance_macro_context",
    },
    {
        "id": "safe_calculation",
        "prompt": "某股现价80元，券商明确目标价100元。请计算上涨空间百分比，并列出公式。",
        "expected_tool": "finance_calculator",
    },
    {
        "id": "relative_limit_up",
        "prompt": "今天哪些A股涨停了？",
        "expected_tool": "finance_limit_up_list",
    },
    {
        "id": "explicit_target_price",
        "prompt": "查询浪潮信息和紫光股份近期券商研报的明确目标价，不要模型估值。",
        "expected_tool": "finance_target_price_history",
    },
    {
        "id": "actual_q1_growth",
        "prompt": "找出2026年Q1实际归母净利润同比增长超过50%的股票。",
        "expected_tool": "finance_financial_growth_screen",
    },
    {
        "id": "forecast_revision",
        "prompt": "分析兆易创新未来两年的研报利润预测变化。",
        "expected_tool": "finance_forecast_trends",
    },
    {
        "id": "price_series",
        "prompt": "查询美的集团和格力电器近两年的复权价格序列。",
        "expected_tool": "finance_price_series",
    },
    {
        "id": "price_chart",
        "prompt": "画一张美的集团和格力电器近两年的归一化股价走势图。",
        "expected_tool": "finance_price_chart",
    },
    {
        "id": "foreign_scope",
        "prompt": "分析AAPL今天的走势。",
        "expected_tool": "finance_entity_resolve",
    },
    {
        "id": "metric_coverage",
        "prompt": "全市场筛选预期利润增速超过50%的股票，并告诉我覆盖率。",
        "expected_tool": "finance_market_screen",
    },
    {
        "id": "company_due_diligence_workflow",
        "prompt": "请对平安银行做一份包含基本面、估值、资金、研报证据和风险缺口的完整尽调。",
        "expected_tool": "finance_research_workflow",
    },
    {
        "id": "temporal_knowledge_graph",
        "prompt": "查询平安银行截至2026年7月16日所属行业、概念暴露和相关事件关系，并给出关系证据。",
        "expected_tool": "finance_knowledge_graph",
    },
    {
        "id": "portfolio_risk",
        "prompt": "组合中平安银行权重60%、贵州茅台40%，计算截至2026年7月16日的历史波动、VaR、回撤和行业集中度。",
        "expected_tool": "finance_portfolio_risk",
    },
    {
        "id": "event_calendar",
        "prompt": "列出2026年7月1日至7月16日贵州茅台的研报和结构化事件日历。",
        "expected_tool": "finance_event_calendar",
    },
    {
        "id": "trusted_backtest",
        "prompt": "用平安银行、贵州茅台、美的集团、格力电器做2026年4月至7月的20日动量回测，10个交易日调仓，并披露偏差。",
        "expected_tool": "finance_strategy_backtest",
    },
    {
        "id": "watchlist_monitor",
        "prompt": "监控平安银行和贵州茅台最近30天的技术信号、研报数量和事件变化。",
        "expected_tool": "finance_watchlist_monitor",
    },
]


def synthetic_result(name: str, args: dict) -> str:
    if name == "finance_current_time":
        return json.dumps({"timezone": "Asia/Shanghai", "date": "2026-07-17", "iso": "2026-07-17T09:30:00+08:00"})
    if name == "finance_entity_resolve":
        query = str(args.get("query") or "")
        if query.upper() == "AAPL":
            return json.dumps({"meta": {"coverage_status": "unsupported_external_market", "row_count": 0}, "rows": []})
        known_entities = {
            "平安银行": ("000001", "平安银行"),
            "贵州茅台": ("600519", "贵州茅台"),
            "美的集团": ("000333", "美的集团"),
            "格力电器": ("000651", "格力电器"),
            "浪潮信息": ("000977", "浪潮信息"),
            "紫光股份": ("000938", "紫光股份"),
            "兆易创新": ("603986", "兆易创新"),
        }
        symbol, canonical_name = known_entities.get(query, (query if query.isdigit() else "000001", query or "测试公司"))
        return json.dumps({
            "meta": {"coverage_status": "local_market_covered", "row_count": 1},
            "rows": [{"entity_type": "asset", "symbol": symbol, "canonical_name": canonical_name}],
        }, ensure_ascii=False)
    if name == "finance_data_context":
        return json.dumps({
            "meta": {"row_count": 1},
            "rows": [{"context_type": "metric", "context_key": "actual_vs_forecast", "description": "actual and forecast must remain separate"}],
        })
    return json.dumps({"meta": {"row_count": 1, "synthetic": True}, "rows": [{"ok": True}]})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=ROOT / "MMClaw/workspaces/qq_agent")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case", action="append", dest="case_ids", help="Run only the named case; repeatable.")
    args = parser.parse_args()

    config = json.loads((args.workspace / "mmclaw.json").read_text(encoding="utf-8"))
    ConfigManager.mode = "qq"
    prompt = ConfigManager.get_full_prompt(config)
    tools = get_native_tool_schemas(config)
    engine = Engine(config)
    rows = []
    selected_cases = [case for case in CASES if not args.case_ids or case["id"] in set(args.case_ids)]
    for case in selected_cases:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": case["prompt"]},
        ]
        names = []
        answered_without_tool = False
        for _ in range(4):
            response = engine.ask(messages, tools=tools, retry=1)
            calls = response.get("tool_calls") or []
            messages.append(response)
            if not calls:
                answered_without_tool = True
                break
            round_names = [call.get("name") or "" for call in calls]
            names.extend(round_names)
            if case["expected_tool"] in round_names:
                break
            results = [synthetic_result(call.get("name") or "", call.get("args") or {}) for call in calls]
            messages.extend(engine.tool_result_messages(calls, results))
        rows.append({
            "id": case["id"],
            "expected_tool": case["expected_tool"],
            "first_tool": names[0] if names else "",
            "all_tools": names,
            "passed": case["expected_tool"] in names,
            "answered_without_tool": answered_without_tool,
        })

    result = {
        "provider": config.get("engine_type"),
        "model": (config.get("engines") or {}).get(config.get("engine_type"), {}).get("model"),
        "passed": sum(row["passed"] for row in rows),
        "total": len(rows),
        "cases": rows,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
