"""Compiler for the deliberately small, auditable factor expression DSL."""
from __future__ import annotations

import math
from typing import Any


FEATURES = {
    "momentum_20d",
    "reversal_5d",
    "low_volatility_20d",
    "volume_surprise_20d",
    "amount_surprise_20d",
    "drawdown_20d",
    "money_flow_intensity_5d",
    "research_breadth_20d",
    "rating_upgrade_20d",
    "event_sentiment_5d",
    "graph_direct_exposure",
    "graph_event_impulse_30d",
    "graph_attention_30d",
    "graph_novelty_30d",
    "graph_rating_momentum_60d",
    "graph_forecast_eps_growth",
    "graph_forecast_revenue_growth",
    "graph_forecast_eps_revision_90d",
    "graph_forecast_revenue_revision_90d",
    "graph_forecast_dispersion_180d",
    "graph_forward_earnings_yield",
    "graph_target_price_upside",
    "graph_policy_impulse_30d",
    "graph_demand_impulse_30d",
    "graph_order_impulse_30d",
    "graph_pricing_impulse_30d",
    "graph_risk_impulse_30d",
    "graph_source_diversity_30d",
    "graph_two_hop_exposure",
    "graph_centrality",
    "graph_evidence_diversity",
    "graph_relation_stability",
}


def compile_expression(node: dict[str, Any], alias: str = "f") -> str:
    if not isinstance(node, dict):
        raise ValueError("factor expression node must be an object")
    op = node.get("op")
    if op == "field":
        field = node.get("field")
        if field not in FEATURES:
            raise ValueError(f"unsupported feature: {field!r}")
        return f'{alias}."{field}"'
    if op == "constant":
        value = float(node.get("value"))
        if not math.isfinite(value):
            raise ValueError("constant must be finite")
        return repr(value)
    if op == "neg":
        return f"(-({compile_expression(node['arg'], alias)}))"
    if op in {"add", "subtract", "multiply", "divide"}:
        left = compile_expression(node["left"], alias)
        right = compile_expression(node["right"], alias)
        symbol = {"add": "+", "subtract": "-", "multiply": "*"}[op] if op != "divide" else "/"
        if op == "divide":
            right = f"NULLIF(({right}),0)"
        return f"(({left}) {symbol} ({right}))"
    if op == "signed_log1p":
        arg = compile_expression(node["arg"], alias)
        return f"(sign({arg}) * ln(1 + abs({arg})))"
    if op == "zscore":
        arg = compile_expression(node["arg"], alias)
        return (
            f"CASE WHEN ({arg}) IS NULL THEN NULL ELSE "
            f"COALESCE((({arg}) - avg({arg}) OVER (PARTITION BY {alias}.dt)) / "
            f"NULLIF(stddev_samp({arg}) OVER (PARTITION BY {alias}.dt),0), 0.0) END"
        )
    if op == "clip":
        arg = compile_expression(node["arg"], alias)
        lower = float(node["lower"])
        upper = float(node["upper"])
        if not all(math.isfinite(x) for x in (lower, upper)) or lower > upper:
            raise ValueError("invalid clip bounds")
        return f"CASE WHEN ({arg}) IS NULL THEN NULL ELSE greatest({repr(lower)}, least({repr(upper)}, ({arg}))) END"
    raise ValueError(f"unsupported factor operation: {op!r}")
