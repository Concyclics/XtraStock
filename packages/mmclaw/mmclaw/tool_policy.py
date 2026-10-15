"""Tool exposure policy for MMClaw.

The policy is enforced in two places:
- tool_schemas hides disabled tools from native tool calling;
- kernel blocks disabled tools at execution time, including JSON tool calls.
"""

from __future__ import annotations


FINANCE_PARAMETERIZED_TOOLS = [
    "finance_time_context",
    "finance_resolve_entities",
    "finance_data_status",
    "finance_market_series_batch",
    "finance_fundamentals_batch",
    "finance_factor_analysis",
    "finance_graph_analysis",
    "finance_research_bundle",
    "finance_research_intelligence",
    "finance_claim_verify",
    "finance_current_time",
    "finance_calculator",
    "finance_plot_chart",
    "finance_db_schema",
    "finance_theme_map",
    "finance_price_movers",
    "finance_research_momentum",
    "finance_screen_stocks",
    "finance_stock_profile",
    "finance_asset_snapshot",
    "finance_fundamental_analysis",
    "finance_peer_compare",
    "finance_relative_valuation",
    "finance_capital_flow_analysis",
    "finance_macro_context",
    "finance_factor_catalog",
    "finance_factor_profile",
    "finance_factor_validation",
    "finance_factor_readiness",
    "finance_factor_attribution",
    "finance_research_workflow",
    "finance_knowledge_graph",
    "finance_graph_paths",
    "finance_event_calendar",
    "finance_portfolio_risk",
    "finance_watchlist_monitor",
    "finance_strategy_backtest",
    "finance_entity_resolve",
    "finance_data_context",
    "finance_market_screen",
    "finance_document_retrieve",
    "finance_document_search",
    "finance_document_bm25_search",
    "finance_topic_clusters",
    "finance_document_evidence",
    "finance_subindustry_trends",
    "finance_company_exposure",
    "finance_forecast_trends",
    "finance_event_study",
    "finance_financial_growth_screen",
    "finance_event_return",
    "finance_fact_check_stock",
    "finance_target_price_history",
    "finance_price_series",
    "finance_price_chart",
    "finance_limit_up_list",
    "finance_data_health",
]

# External chat agents should get one clear document-retrieval entry point rather
# than three near-duplicate ranking/debug tools. The omitted tools remain
# available to the finance_research profile for diagnostics.
FINANCE_CHAT_READONLY_TOOLS = [
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
    "finance_calculator",
    "finance_price_chart",
]


TOOL_PROFILES = {
    "default": {
        "description": "No additional tool restrictions beyond normal mode/browser availability.",
        "allowed": [],
        "disabled": [],
    },
    "admin": {
        "description": "No additional tool restrictions. Intended for trusted interactive maintenance.",
        "allowed": [],
        "disabled": [],
    },
    "finance_daily_safe": {
        "description": "Safe daily finance-report profile: no shell, browser, cron mutation, memory mutation, upgrade, or raw SQL.",
        "allowed": [
            "file_read",
            "file_write",
            "wait",
            *FINANCE_PARAMETERIZED_TOOLS,
        ],
        "disabled": [
            "finance_db_query",
            "shell_execute",
            "shell_async",
            "file_upload",
            "upgrade",
            "cron_create",
            "cron_delete",
            "memory_add",
            "memory_delete",
            "browser_*",
        ],
    },
    "finance_chat_readonly": {
        "description": "External finance-chat profile: parameterized read-only finance queries only; no files, shell, browser, raw SQL, or memory mutation.",
        "allowed": [
            "wait",
            *FINANCE_CHAT_READONLY_TOOLS,
        ],
        "disabled": [
            "file_*",
            "finance_db_query",
            "shell_execute",
            "shell_async",
            "upgrade",
            "cron_create",
            "cron_delete",
            "memory_add",
            "memory_delete",
            "browser_*",
        ],
    },
    "finance_research": {
        "description": "Interactive finance research profile: finance tools, raw read-only SQL, files, wait, and memory; no shell/browser/upgrade/cron mutation.",
        "allowed": [
            "file_read",
            "file_write",
            "wait",
            "memory_add",
            "memory_list",
            "memory_delete",
            "finance_*",
        ],
        "disabled": [
            "shell_execute",
            "shell_async",
            "file_upload",
            "upgrade",
            "cron_create",
            "cron_delete",
            "browser_*",
        ],
    },
    "finance_readonly": {
        "description": "Read-only finance profile: finance tools, raw read-only SQL, file_read, and wait only.",
        "allowed": [
            "file_read",
            "wait",
            "finance_*",
        ],
        "disabled": [
            "file_write",
            "file_upload",
            "shell_execute",
            "shell_async",
            "upgrade",
            "cron_create",
            "cron_delete",
            "memory_add",
            "memory_delete",
            "browser_*",
        ],
    },
}


def _as_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(x) for x in value if str(x)]
    return []


def _matches(name: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern == name:
            return True
        if pattern.endswith("*") and name.startswith(pattern[:-1]):
            return True
    return False


def resolve_tool_policy(config: dict | None) -> dict:
    config = config or {}
    profile_name = str(config.get("tool_profile") or "default")
    profile = TOOL_PROFILES.get(profile_name, TOOL_PROFILES["default"])

    allowed = _as_list(profile.get("allowed"))[:]
    disabled = _as_list(profile.get("disabled"))[:]
    explicit_allowed = _as_list(config.get("allowed_tools"))
    explicit_disabled = _as_list(config.get("disabled_tools"))

    if explicit_allowed:
        allowed = explicit_allowed
    disabled.extend(explicit_disabled)

    return {
        "profile": profile_name if profile_name in TOOL_PROFILES else "default",
        "description": profile.get("description", ""),
        "allowed": allowed,
        "disabled": disabled,
        "explicit_allowed": explicit_allowed,
        "explicit_disabled": explicit_disabled,
    }


def is_tool_enabled(name: str, config: dict | None) -> bool:
    policy = resolve_tool_policy(config)
    allowed = policy["allowed"]
    disabled = policy["disabled"]
    if allowed and not _matches(name, allowed):
        return False
    if disabled and _matches(name, disabled):
        return False
    return True


def filter_tool_schemas(tools: list[dict], config: dict | None) -> list[dict]:
    return [
        tool
        for tool in tools
        if is_tool_enabled(tool.get("function", {}).get("name", ""), config)
    ]


def describe_tool_policy(config: dict | None) -> str:
    policy = resolve_tool_policy(config)
    if policy["profile"] in ("default", "admin") and not policy["explicit_allowed"] and not policy["explicit_disabled"]:
        return ""
    lines = [
        "[TOOL POLICY]",
        f"Active tool_profile: {policy['profile']}",
        policy["description"],
    ]
    if policy["allowed"]:
        lines.append("Allowed tool patterns: " + ", ".join(policy["allowed"]))
    if policy["disabled"]:
        lines.append("Disabled tool patterns: " + ", ".join(policy["disabled"]))
    lines.append("This policy is authoritative: disabled tools are unavailable even if mentioned elsewhere in the prompt.")
    return "\n".join(line for line in lines if line) + "\n\n"
