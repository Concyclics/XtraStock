"""Deterministic intent routing for the public finance agent tool surface."""

from __future__ import annotations

import re


PUBLIC_FINANCE_V3_TOOLS = [
    "finance_time_context",
    "finance_resolve_entities",
    "finance_data_status",
    "finance_market_screen",
    "finance_market_series_batch",
    "finance_fundamentals_batch",
    "finance_document_retrieve",
    "finance_factor_analysis",
    "finance_graph_analysis",
    "finance_research_bundle",
    "finance_research_intelligence",
    "finance_claim_verify",
    "finance_price_chart",
]


ROUTES = {
    "market": (
        r"股价|行情|走势|涨跌|跌幅|涨幅|K线|k线|收盘|开盘|成交|PE|PB|市盈率|市净率|筛选|排名",
        ["finance_market_screen", "finance_market_series_batch"],
    ),
    "fundamentals": (
        r"财报|业绩|营收|收入|利润|净利|现金流|ROE|EPS|同比|环比|一季报|半年报|年报|actual|forecast|consensus|预测",
        ["finance_fundamentals_batch"],
    ),
    "documents": (
        r"新闻|消息|公告|研报|报告|舆情|政策|日报|资讯|原文|证据|来源",
        ["finance_document_retrieve"],
    ),
    "factor": (
        r"因子|信号|IC|回测|动量|反转|波动率|归因|分位数|zscore|预测方案",
        ["finance_factor_analysis"],
    ),
    "graph": (
        r"图谱|产业链|上下游|板块|行业|概念|主题|关联|供应链|AI硬件|算力|半导体|光模块|存储|PCB|CPO|MPO|HBM",
        ["finance_graph_analysis", "finance_document_retrieve"],
    ),
    "research": (
        r"分析|研究|建议|推荐|投资|综合|逻辑|机会|风险|比较|对比",
        ["finance_research_bundle", "finance_research_intelligence"],
    ),
    "chart": (
        r"画图|图表|曲线|可视化|走势图",
        ["finance_market_series_batch", "finance_price_chart"],
    ),
    "verify": (
        r"核实|验证|查错|幻觉|是否正确|证据编号|evidence",
        ["finance_claim_verify"],
    ),
}


def route_finance_tools(text: str | None, *, max_tools: int = 10) -> list[str]:
    value = str(text or "")
    selected = ["finance_time_context", "finance_resolve_entities", "finance_data_status"]
    matched = False
    for pattern, tools in ROUTES.values():
        if re.search(pattern, value, flags=re.I):
            matched = True
            selected.extend(tools)
    if not matched:
        selected.extend([
            "finance_research_bundle", "finance_document_retrieve", "finance_market_screen",
        ])
    # Research answers often need a concise all-in-one fallback, while simple
    # direct lookups do not need it added implicitly.
    if len(selected) < 6 and re.search(r"股票|公司|市场|金融|财经", value):
        selected.append("finance_research_bundle")
    if re.search(r"热点|低估|板块.*分析|分析.*板块|个股.*走势", value):
        selected.extend(["finance_market_screen", "finance_market_series_batch",
                         "finance_fundamentals_batch", "finance_document_retrieve"])
    result = []
    for name in selected:
        if name in PUBLIC_FINANCE_V3_TOOLS and name not in result:
            result.append(name)
    return result[:max_tools]


def filter_routed_schemas(tools: list[dict], text: str | None) -> list[dict]:
    allowed = set(route_finance_tools(text))
    return [tool for tool in tools if tool.get("function", {}).get("name") in allowed]
