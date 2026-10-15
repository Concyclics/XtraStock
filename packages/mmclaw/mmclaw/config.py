import json
import os
import shutil
from pathlib import Path
import platform
from .tools import ShellTool
from .reader_language import READER_STYLE_PROMPT
from .tool_policy import describe_tool_policy, is_tool_enabled
from .memory import MAX_MEMORY_ENTRY_CHARS, MAX_TOTAL_MEMORY_CHARS

def _find_file_icase(directory: Path, name: str):
    """Return path to `name` inside `directory`, matching case-insensitively.
    Returns the exact-case path if it exists, otherwise the first case-insensitive match,
    or None if not found."""
    exact = directory / name
    if exact.exists():
        return exact
    name_lower = name.lower()
    try:
        for entry in directory.iterdir():
            if entry.name.lower() == name_lower:
                return entry
    except Exception:
        pass
    return None


def set_workspace(path: Path):
    """Override the default workspace directory. Must be called before any config/skill access."""
    path = path.expanduser().resolve()
    SkillManager.HOME_DIR = path
    SkillManager.HOME_SKILLS_DIR = path / "skills"
    SkillManager.HOME_KG_DIR = path / "skill-kg"
    SkillManager.HOME_KG_MAIN = path / "skill-kg" / "skill-kg-main.md"
    SkillManager.HOME_KG_USER = path / "skill-kg" / "skill-kg-user.md"
    ConfigManager.CONFIG_DIR = path
    ConfigManager.CONFIG_FILE = path / "mmclaw.json"
    # Patch other modules (late imports are safe — all modules are loaded before main() calls this)
    from .memory import FileMemory, GlobalFileMemory
    FileMemory.SESSIONS_DIR = str(path / "memory" / "sessions")
    GlobalFileMemory.GLOBAL_MEMORY_FILE = str(path / "memory" / "global" / "memory.jsonl")
    from .kernel import HeartbeatManager, CronManager
    HeartbeatManager.HEARTBEAT_DIR = path / "heartbeat"
    HeartbeatManager.CONFIG_FILE = path / "heartbeat" / "heartbeat-config.json"
    HeartbeatManager.LOG_FILE = path / "heartbeat" / "heartbeat-log.jsonl"
    HeartbeatManager.SKILLS_DIR = path / "skills"
    CronManager.CRON_DIR  = path / "cron"
    CronManager.JOBS_FILE = path / "cron" / "cron-jobs.jsonl"
    from .watcher import WatcherManager
    WatcherManager.SKILLS_DIR = path / "skills"
    from .tools import BrowserTool
    BrowserTool.DEFAULT_DATA_DIR = str(path / "browser_data")


class SkillManager(object):
    HOME_DIR = Path.home() / ".mmclaw"
    HOME_SKILLS_DIR = Path.home() / ".mmclaw" / "skills"
    PKG_SKILLS_DIR = Path(__file__).parent / "skills"
    PKG_KG_DIR = Path(__file__).parent / "skill-kg"
    HOME_KG_DIR = Path.home() / ".mmclaw" / "skill-kg"
    HOME_KG_MAIN = Path.home() / ".mmclaw" / "skill-kg" / "skill-kg-main.md"
    HOME_KG_USER = Path.home() / ".mmclaw" / "skill-kg" / "skill-kg-user.md"

    _cache_prompt = None
    _cache_mtime = 0

    @classmethod
    def sync_skills(cls):
        """Copy skill directories and KG files from package to ~/.mmclaw/."""
        cls.HOME_DIR.mkdir(parents=True, exist_ok=True)
        if cls.HOME_SKILLS_DIR.exists() is False:
            cls.HOME_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        if cls.PKG_SKILLS_DIR.exists():
            for skill_dir in cls.PKG_SKILLS_DIR.iterdir():
                if not skill_dir.is_dir():
                    continue
                dest = cls.HOME_SKILLS_DIR / skill_dir.name
                shutil.copytree(skill_dir, dest, dirs_exist_ok=True)

        # Sync KG dir (always overwrite main, never touch user file)
        cls.HOME_KG_DIR.mkdir(parents=True, exist_ok=True)
        if cls.PKG_KG_DIR.exists():
            shutil.copy2(cls.PKG_KG_DIR / "skill-kg-main.md", cls.HOME_KG_MAIN)

        # Create user KG file only if it doesn't exist yet
        if not cls.HOME_KG_USER.exists():
            cls.HOME_KG_USER.write_text(
                "# MM-SkillKG — User Custom Relations\n"
                "# Add your own relations here. This file is never overwritten by mmclaw updates.\n"
                "# Format: entity_a, [relation], entity_b  # optional comment\n\n",
                encoding="utf-8"
            )

    @classmethod
    def _parse_kg_file(cls, path):
        """Parse a .md KG file into a list of (a, relation, b, comment) tuples."""
        triples = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                comment = ""
                if "#" in line:
                    line, _, comment = line.partition("#")
                    comment = comment.strip()
                parts = [p.strip() for p in line.split(",")]
                if len(parts) != 3:
                    continue
                a, rel, b = parts
                rel = rel.strip("[]")
                if a and rel and b:
                    triples.append((a, rel, b, comment))
        except Exception:
            pass
        return triples

    @classmethod
    def get_skill_kg_prompt(cls):
        """Load and merge main + user KG files into a prompt section."""
        triples = cls._parse_kg_file(cls.HOME_KG_MAIN) + cls._parse_kg_file(cls.HOME_KG_USER)
        if not triples:
            return ""
        lines = [f"- {a} [{rel}] {b}" + (f"  # {c}" if c else "") for a, rel, b, c in triples]
        return (
            "\n\n[SKILL KNOWLEDGE GRAPH]\n"
            "These are known relations between skills and concepts. "
            "Use them to reason about dependencies and safety before activating a skill.\n\n"
        ) + "\n".join(lines) + "\n"

    @classmethod
    def _parse_frontmatter(cls, text):
        """Return (meta_dict, body) parsed from YAML-style frontmatter."""
        if not text.startswith("---"):
            return {}, text
        parts = text.split("---", 2)
        if len(parts) < 3:
            return {}, text
        meta = {}
        for line in parts[1].splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                meta[key.strip()] = val.strip()
        return meta, parts[2].strip()

    @classmethod
    def get_skills_prompt(cls, force=False):
        """Build a lightweight skills index for the system prompt.

        Uses mtime caching and partial reads to avoid redundant filesystem scans.
        """
        if not cls.HOME_SKILLS_DIR.exists():
            return ""

        try:
            # Detect additions, removals, and changes to any skill.md
            current_mtime = cls.HOME_SKILLS_DIR.stat().st_mtime
            for skill_dir in cls.HOME_SKILLS_DIR.iterdir():
                if skill_dir.is_dir():
                    skill_file = _find_file_icase(skill_dir, "skill.md")
                    if skill_file:
                        current_mtime = max(current_mtime, skill_file.stat().st_mtime)

            if not force and cls._cache_prompt is not None and current_mtime <= cls._cache_mtime:
                return cls._cache_prompt
        except Exception:
            current_mtime = 0

        # Only print if this isn't the first time loading (bootup)
        if cls._cache_prompt is not None:
            print("[*] Skill update detected.")

        entries = []
        # Sort by directory name for stable prompt (LLM KV cache friendly)
        for skill_dir in sorted(cls.HOME_SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = _find_file_icase(skill_dir, "skill.md")
            if not skill_file:
                continue
            try:
                # Read only first 2KB for frontmatter to save IO
                with open(skill_file, "r", encoding="utf-8") as f:
                    head = f.read(2048)
                meta, _ = cls._parse_frontmatter(head)
            except Exception:
                continue

            # Defensive: only include skills that are "ready" (have a name)
            name = meta.get("name")
            if not name:
                continue

            description = meta.get("description", "")
            entries.append(f"- name: {name}\n  description: {description}\n  path: {skill_file}")

        if not entries:
            cls._cache_prompt = ""
            cls._cache_mtime = current_mtime
            return ""

        skills_text = (
            "\n\n[SKILLS SECTION]\n"
            "The following skills are available. Do NOT execute a skill unless the user's request requires it.\n"
            "To get full instructions for a skill, call file_read(<path>) before using it.\n\n"
            "Available Skills:\n"
        ) + "\n".join(entries) + "\n"

        cls._cache_prompt = skills_text
        cls._cache_mtime = current_mtime
        return skills_text

class ConfigManager(object):
    mode = "terminal"
    stateless_use_global_memory = False

    BASE_SYSTEM_PROMPT = (
        "You are MMClaw, an autonomous AI agent. "
        "You MUST always respond with a SINGLE valid JSON object. "
        "Do not include any text outside the JSON block.\n\n"
        "IMPORTANT: When you use 'tools', you MUST STOP your response immediately after the JSON block. "
        "Do not simulate the tool output. Wait for the system to provide the result.\n\n"
        "Structure:\n"
        "{\n"
        "  \"thought\": \"your reasoning\",\n"
        "  \"tools\": [\n"
        "    {\"name\": \"tool_name\", \"args\": {\"arg1\": \"val1\"}}\n"
        "  ],\n"
        "  \"content\": \"message to user\"\n"
        "}\n"
        "IMPORTANT: \"content\" MUST be a plain string. Never nest JSON objects or arrays inside \"content\".\n\n"
        "Available Tools:\n"
        f"- shell_execute(command): Executes a command and returns the output. Times out after {ShellTool.TIMEOUT}s. Use this for tasks that finish quickly.\n"
        "- shell_async(command): Starts a long-running command (like a server or listener) in the background. Does not return output. "
        "IMPORTANT: Do NOT append ' &' to the command; the tool handles backgrounding automatically.\n"
        "- file_read(path)\n"
        "- file_write(path, content)\n"
        "- file_upload(path)\n"
        "- finance_current_time(timezone?): Returns the current date/time. Use before resolving today, yesterday, this week, recent, latest, current, or now.\n"
        "- finance_plot_chart(code, filename?, width?, height?, dpi?): Generates a PNG chart with pandas/seaborn/matplotlib and returns a local path. It automatically loads the configured Chinese CJK font. Use file_upload(path) afterward to share it.\n"
        "- finance_db_schema(schema?, table?): Lists local finance database tables or columns.\n"
        "- finance_db_query(sql, limit?): Runs one read-only SELECT/WITH SQL query against the local finance database.\n"
        "- finance_theme_map(theme, limit?): Maps a theme to AI-hardware concepts and sample stocks.\n"
        "- finance_price_movers(days, threshold, direction, theme?, limit?): Finds recent large price movers.\n"
        "- finance_research_momentum(days, theme?, stock_code?, limit?): Ranks recent research/rating momentum.\n"
        "- finance_screen_stocks(filters): Screens stocks by theme, PE, research momentum, and technical returns.\n"
        "- finance_stock_profile(stock_code?, stock_name?, days?, limit?): Builds a compact stock profile.\n"
        "- finance_asset_snapshot(as_of_date?, stock_code?, stock_name?, limit?): Reads a point-in-time all-market stock snapshot; always report returned component dates.\n"
        "- finance_document_retrieve(query, keywords?, source_types?, days?, stock_code?, intent?, require_all_terms?, include_body?, limit?, candidate_limit?, backend?): Default hybrid retrieval. ranked/evidence fuse Meilisearch or Postgres FTS with local BGE embeddings; disclose semantic coverage while backfill is incomplete.\n"
        "- finance_research_workflow(workflow_type, stock_code?, stock_name?, theme?, peer_stock_codes?, as_of_date?, days?, limit?): Deterministic company due diligence, peer comparison, or theme research with evidence IDs and completeness verification.\n"
        "- finance_knowledge_graph(query?, stock_code?, relation_types?, as_of_date?, start_date?, end_date?, known_at?, knowledge_mode?, query_mode?, lookback_days?, limit?): Bitemporal company, industry, concept, event, rating, forecast, and broker relationships with evidence. Default knowledge_mode=public.\n"
        "- finance_watchlist_monitor(stock_codes?, watchlist_name?, days?, limit?): Current technical, signal, report, and event state for a watchlist.\n"
        "- finance_portfolio_risk(holdings?, portfolio_name?, as_of_date?, lookback_days?): Historical portfolio volatility, VaR, drawdown, and concentration from canonical qfq prices.\n"
        "- finance_event_calendar(start_date?, end_date?, stock_code?, event_types?, include_reports?, limit?): Unified dated reports and high-quality events.\n"
        "- finance_strategy_backtest(strategy, start_date, end_date, stock_codes, top_n?, rebalance_days?, transaction_cost_bps?): Bounded canonical-price research backtest; disclose all returned biases.\n"
        "- finance_document_search/document_bm25_search/document_evidence(...): Advanced retrieval and backend-diagnosis tools; prefer finance_document_retrieve for normal analysis.\n"
        "- finance_topic_clusters(query, keywords?, source_types?, days?, limit?): Clusters matched documents by keywords/events.\n"
        "- finance_subindustry_trends(subindustry?, days?, limit?): Ranks AI-hardware fine-grained subindustry trend changes.\n"
        "- finance_company_exposure(query?, term?, subindustry?, stock_code?, stock_name?, limit?): Finds companies exposed to AI-hardware terms/subindustries.\n"
        "- finance_forecast_trends(stock_code?, stock_name?, subindustry?, days?, limit?): Analyzes revenue/net-profit/EPS/PE forecast revisions.\n"
        "- finance_event_study(query?, keywords?, event_type?, stock_code?, days?, post_days?, limit?): Inspects event-linked stocks and current technical-state proxy; it is not an event-date return backtest.\n"
        "- finance_financial_growth_screen(report_period?, metric?, growth_gt?, growth_lt?, return_lte?, return_gte?, latest_date?, price_adjustment?, limit?): Screens actual reported financial growth and same-source report-date returns; use for 一季报/Q1/归母净利润同比, not forecasts.\n"
        "- finance_event_return(stock_code?, stock_name?, event_type?, event_date?, price_adjustment?, limit?): Reads materialized event-date to latest-date returns without mixing price sources. Current deployment uses analytics.asset_technical_daily as the available same-source daily close series.\n"
        "- finance_fact_check_stock(stock_code?, stock_name?, report_period?, price_adjustment?, days?, limit?): Fact-checks one stock's actual metrics, event returns, and recent report rows.\n"
        "- finance_data_health(limit?): Checks finance data freshness and pipeline health.\n"
        "Accuracy rule: for disclosed results or report-date returns, use finance_financial_growth_screen/finance_event_return/finance_fact_check_stock; never substitute forecast rows or hard-coded numbers.\n"
        "- wait(seconds)\n\n"
    )

    NATIVE_SYSTEM_PROMPT = (
        "You are MMClaw, an autonomous AI agent. "
        "Use the provided native tools when tool use is needed. "
        "Do not emit JSON for tool calls and do not include a self-defined 'thought' field. "
        "When no tool is needed, answer the user directly in plain text.\n\n"
        "Tool use policy:\n"
        "- Use tools to inspect files, run commands, manage sessions, browse, or complete tasks that require external actions.\n"
        "- After requesting a tool, wait for the tool result before continuing.\n"
        "- For long-running or blocking commands, use shell_async instead of shell_execute.\n\n"
    )

    DEFAULT_CONFIG = {
        "engine_type": "openai",
        "tool_calling_mode": "native",
        "tool_profile": "default",
        "allowed_tools": [],
        "disabled_tools": [],
        "tool_control": {
            "chat_max_rounds": 6,
            "chat_max_calls": 12,
            "background_max_rounds": 10,
            "background_max_calls": 30,
            "duplicate_limit": 1,
            "same_error_limit": 2,
        },
        "finance_v3": {
            "answer_guard_enabled": True,
            "max_public_tools_per_turn": 10,
            "simple_timeout_seconds": 3,
            "retrieval_timeout_seconds": 8,
            "research_timeout_seconds": 20,
            "agentic_loop": {
                "enabled": True,
                "few_shot_enabled": True,
                "max_stages": 4,
                "max_model_calls": 28,
                "max_seconds": 600,
                "max_reviews": 6,
                "max_selected_tools": 8,
            },
        },
        "browser": {
            "enabled": False,
            "data_dir": "~/.mmclaw/browser_data",
        },
        "engines": {
            "openai": {
                "model": "gpt-4o",
                "api_key": None,
                "base_url": "https://api.openai.com/v1"
            },
            "codex": {
                "model": "gpt-5.2",
                "api_key": None,
                "base_url": "https://api.openai.com/v1"
            },
            "deepseek": {
                "model": "deepseek-chat",
                "api_key": None,
                "base_url": "https://api.deepseek.com"
            },
            "openrouter": {
                "model": "anthropic/claude-3.5-sonnet",
                "api_key": None,
                "base_url": "https://openrouter.ai/api/v1"
            },
            "kimi": {
                "model": "kimi-k2.5",
                "api_key": None,
                "base_url": "https://api.moonshot.cn/v1"
            },
            "google": {
                "model": "gemini-1.5-pro",
                "api_key": None,
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"
            },
            "vertex_ai": {
                "model": "gemini-2.5-flash",
                "api_key": None,
                "base_url": "https://aiplatform.googleapis.com/v1/publishers/google"
            }
        },
        "connector_type": "terminal",
        "connectors": {
            "telegram": {
                "token": None,
                "authorized_user_id": 0
            },
            "whatsapp": {
                "authorized_id": None
            },
            "wechat": {
                "token": None,
                "base_url": "https://ilinkai.weixin.qq.com",
                "authorized_id": None,
                "account_id": None,
                "get_updates_buf": "",
                "log_messages": True,
                "log_dir": None,
                "usage_dir": None,
                "allowed_users": [],
                "blocked_users": [],
                "admin_users": [],
                "auto_admin_authorized_user": True,
                "default_daily_budget_per_user": 10,
                "default_min_interval_seconds": 0,
                "member_limits": {},
                "notify_on_limit": True,
                "suppress_progress_messages": True,
                "max_text_chars": 900,
                "default_user_id": None,
                "daily_report_push_enabled": False,
                "daily_report_time": "09:05",
                "daily_report_dir": None,
                "daily_report_user_ids": [],
                "daily_report_user_id": None,
                "daily_report_to_authorized_user": True,
                "daily_report_max_chars": 3500,
                "daily_report_brief_enabled": True,
                "daily_report_brief_input_chars": 18000
            },
            "feishu": {
                "app_id": None,
                "app_secret": None,
                "authorized_id": None
            },
            "qqbot": {
                "app_id": None,
                "app_secret": None,
                "log_messages": True,
                "log_dir": None,
                "usage_dir": None,
                "respond_in_groups": "mention",
                "group_require_command_prefix": False,
                "group_command_prefixes": ["/", "!", "！"],
                "allowed_groups": [],
                "blocked_groups": [],
                "allowed_users": [],
                "blocked_users": [],
                "admin_users": [],
                "default_daily_budget_per_member": 0,
                "default_daily_budget_per_group": 0,
                "default_min_interval_seconds": 0,
                "member_limits": {},
                "notify_on_limit": True,
                "default_group_openid": None,
                "daily_push_group_openid": None,
                "default_user_openid": None,
                "daily_report_push_enabled": False,
                "daily_report_time": "09:05",
                "daily_report_dir": None,
                "daily_report_group_openids": [],
                "daily_report_group_openid": None,
                "daily_report_max_chars": 3500,
                "daily_report_brief_enabled": True,
                "daily_report_brief_input_chars": 18000,
                "file_server_dir": None,
                "file_server_port": 8788,
                "file_public_base_url": None
            }
        }
    }
    CONFIG_DIR = Path.home() / ".mmclaw"
    CONFIG_FILE = CONFIG_DIR / "mmclaw.json"

    @classmethod
    def load(cls):
        if not cls.CONFIG_DIR.exists():
            cls.CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        if not cls.CONFIG_FILE.exists():
            return None

        try:
            config = json.load(open(cls.CONFIG_FILE, "r", encoding="utf-8"))
            needs_save = False

            # Migration: preferred_mode -> connector_type
            if "preferred_mode" in config:
                print("[*] Migrating 'preferred_mode' to 'connector_type'...")
                config["connector_type"] = config.pop("preferred_mode")
                needs_save = True

            # Migration: Engines
            if "engines" not in config:
                print("[*] Migrating legacy engine configuration...")
                new_engines = {}
                legacy_map = {1: "openai", 2: "deepseek", 3: "openrouter", 4: "openai_compatible"}

                e_type = config.get("engine_type", "openai")
                if isinstance(e_type, int):
                    e_type = legacy_map.get(e_type, "openai")

                active_engine_config = {
                    "model": config.get("model", cls.DEFAULT_CONFIG["engines"]["openai"]["model"]),
                    "api_key": config.get("api_key", "sk-xxx"),
                    "base_url": config.get("base_url", "https://api.openai.com/v1")
                }

                for k, v in cls.DEFAULT_CONFIG["engines"].items():
                    new_engines[k] = v.copy()
                new_engines[e_type] = active_engine_config

                config["engines"] = new_engines
                config["engine_type"] = e_type

                for key in ["model", "api_key", "base_url"]:
                    if key in config: del config[key]
                needs_save = True

            # Migration: openai_compatible (old format) → openai_compatible_default
            if "engines" in config and "openai_compatible" in config["engines"]:
                oc = config["engines"]["openai_compatible"]
                if oc.get("api_key"):
                    print("[*] Migrating 'openai_compatible' → 'openai_compatible_default'...")
                    config["engines"]["openai_compatible_default"] = oc
                    if config.get("engine_type") == "openai_compatible":
                        config["engine_type"] = "openai_compatible_default"
                else:
                    print("[*] Removing empty 'openai_compatible' engine...")
                    if config.get("engine_type") == "openai_compatible":
                        config["engine_type"] = "openai"
                del config["engines"]["openai_compatible"]
                needs_save = True

            # Migration: Fix Google Base URL (add /openai if missing)
            if "engines" in config and "google" in config["engines"]:
                g_config = config["engines"]["google"]
                if g_config.get("base_url") == "https://generativelanguage.googleapis.com/v1beta":
                    print("[*] Updating Google Gemini base_url to OpenAI-compatible endpoint...")
                    g_config["base_url"] = "https://generativelanguage.googleapis.com/v1beta/openai"
                    needs_save = True

            # Migration: Connectors
            if "connectors" not in config:
                print("[*] Migrating legacy connector configuration...")
                config["connectors"] = {
                    "telegram": {
                        "token": config.get("telegram_token", ""),
                        "authorized_user_id": config.get("telegram_authorized_user_id", 0)
                    },
                    "whatsapp": {
                        "authorized_id": config.get("whatsapp_authorized_id")
                    },
                    "feishu": {
                        "app_id": config.get("feishu_app_id", ""),
                        "app_secret": config.get("feishu_app_secret", ""),
                        "authorized_id": config.get("feishu_authorized_id")
                    }
                }
                # Clean up legacy flat keys
                legacy_keys = [
                    "telegram_token", "telegram_authorized_user_id",
                    "whatsapp_authorized_id",
                    "feishu_app_id", "feishu_app_secret", "feishu_authorized_id"
                ]
                for key in legacy_keys:
                    if key in config: del config[key]
                needs_save = True

            if "connectors" not in config:
                config["connectors"] = {}
                needs_save = True
            for connector_name, defaults in cls.DEFAULT_CONFIG.get("connectors", {}).items():
                if connector_name not in config["connectors"]:
                    config["connectors"][connector_name] = defaults.copy()
                    needs_save = True
                elif isinstance(config["connectors"][connector_name], dict):
                    for key, value in defaults.items():
                        if key not in config["connectors"][connector_name]:
                            config["connectors"][connector_name][key] = value
                            needs_save = True

            if "tool_calling_mode" not in config:
                config["tool_calling_mode"] = cls.DEFAULT_CONFIG["tool_calling_mode"]
                needs_save = True
            for key in ["tool_profile", "allowed_tools", "disabled_tools"]:
                if key not in config:
                    config[key] = cls.DEFAULT_CONFIG[key]
                    needs_save = True
            if not isinstance(config.get("tool_control"), dict):
                config["tool_control"] = cls.DEFAULT_CONFIG["tool_control"].copy()
                needs_save = True
            else:
                for key, value in cls.DEFAULT_CONFIG["tool_control"].items():
                    if key not in config["tool_control"]:
                        config["tool_control"][key] = value
                        needs_save = True
            if not isinstance(config.get("finance_v3"), dict):
                config["finance_v3"] = cls.DEFAULT_CONFIG["finance_v3"].copy()
                needs_save = True
            else:
                for key, value in cls.DEFAULT_CONFIG["finance_v3"].items():
                    if key not in config["finance_v3"]:
                        config["finance_v3"][key] = value
                        needs_save = True
            if "native_function_calling_mode" in config:
                del config["native_function_calling_mode"]
                needs_save = True

            if needs_save:
                cls.save(config)

            from xtrastock.config import Settings
            return Settings().agent_config(config)
        except Exception as e:
            print(f"[!] Error loading config: {e}")
            return None

    @classmethod
    def save(cls, config):
        with open(cls.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        print(f"[*] Config saved to {cls.CONFIG_FILE}")

    @classmethod
    def _get_tool_notices_prompt(cls):
        return (
            "IMPORTANT: For long-running or blocking commands (e.g. starting a server, running ngrok, or any process "
            "that does not exit on its own), you MUST use 'shell_async'. "
            "Using 'shell_execute' for these will cause the agent to hang.\n\n"
            "IMPORTANT: When creating files and no destination path is specified by the user, always write to the "
            "system temp directory. The agent's working directory is an internal path with no meaning to the user.\n\n"
        )

    @classmethod
    def _get_tool_policy_prompt(cls, config):
        return describe_tool_policy(config)

    @classmethod
    def _get_finance_db_prompt(cls):
        return (
            "[LOCAL FINANCE DATABASE]\n"
            "You have read-only access to the user's local finance corpus database through native tools.\n"
            "Use these tools for finance research questions that need local data:\n"
            "- finance_current_time(timezone?): return the current date/time. Default timezone is Asia/Shanghai.\n"
            "- finance_plot_chart(code, filename?, width?, height?, dpi?): generate a PNG chart with pandas, seaborn, and matplotlib from data you already retrieved. The returned path can be sent with file_upload(path).\n"
            "- finance_db_schema(schema?, table?): list allowed tables or columns for one table.\n"
            "- finance_db_query(sql, limit?): run exactly one read-only SELECT/WITH query. Default limit is 200 rows, hard cap is 1000 rows.\n\n"
            "Prefer the following parameterized finance tools before writing custom SQL:\n"
            "- finance_screen_stocks(theme?, days?, pe_lt?, pe_gt?, include_negative_pe?, min_reports?, min_positive_ratings?, min_upgrades?, ret_5d_lte?, ret_5d_gte?, ret_20d_lte?, ret_20d_gte?, limit?): screen AI-hardware-scored stocks by valuation, theme, research momentum, and technical returns. When PE filters are used, negative PE is excluded by default unless include_negative_pe=true.\n"
            "- finance_price_movers(days?, threshold?, direction?, theme?, limit?): answer recent price move questions. days=1 uses pct_chg_1d; 2-7 uses ret_5d; 8-30 uses ret_20d. threshold accepts 0.2 or 20 for 20%.\n"
            "- finance_research_momentum(days?, theme?, stock_code?, limit?): rank stocks by recent report_count, positive_rating_count, buy/overweight, upgrades, and first coverage.\n"
            "- finance_theme_map(theme, limit?): map natural language themes like 存储硬件, 光模块, PCB, 液冷, 电源 to concept buckets and sample stocks.\n"
            "- finance_stock_profile(stock_code?, stock_name?, days?, limit?): summarize one stock's theme exposure, valuation, recent research, technical state, and events.\n\n"
            "- finance_asset_snapshot(as_of_date?, stock_code?, stock_name?, limit?): read the all-market point-in-time snapshot and disclose component source dates.\n"
            "AI hardware and signal tools:\n"
            "- finance_subindustry_trends(subindustry?, days?, limit?): rank fine-grained AI hardware subindustries by current-vs-previous mention and research momentum. Use for MLCC, MPO, PCB, 光模块, 玻璃基板, 六氟化钨, 特气, 液冷等细分趋势.\n"
            "- finance_company_exposure(query?, term?, subindustry?, stock_code?, stock_name?, limit?): find companies exposed to a term/subindustry and return evidence, mentions, research coverage, forecast growth, forward PE, and target price.\n"
            "- finance_forecast_trends(stock_code?, stock_name?, subindustry?, days?, limit?): analyze forecast revisions from research reports for revenue, net profit, EPS, and PE.\n"
            "- finance_event_study(query?, keywords?, event_type?, stock_code?, days?, post_days?, limit?): inspect extracted document events linked to stocks and attach the latest precomputed 1d/5d/20d technical-state proxy. It is not an event-date return backtest; use finance_event_return for supported report-date events.\n"
            "- finance_financial_growth_screen(report_period?, metric?, growth_gt?, growth_lt?, return_lte?, return_gte?, latest_date?, price_adjustment?, limit?): screen actual extracted quarterly financial statement metrics and same-source event returns. Use this for 一季报/Q1/归母净利润同比/营业收入同比/业绩高增但报告日至今未涨. This is actual historical result extraction, not forecast rows.\n"
            "- finance_event_return(stock_code?, stock_name?, event_type?, event_date?, price_adjustment?, limit?): read materialized report-date/event-date to latest-trade-date returns from finalized canonical daily bars. Use this when comparing 报告日价 vs 最新价.\n"
            "- finance_fact_check_stock(stock_code?, stock_name?, report_period?, price_adjustment?, days?, limit?): fact-check one stock with actual extracted metrics, event returns, and recent report rows. Use when a prior or suspicious answer may have mixed forecasts, actuals, or price sources.\n"
            "- finance_data_health(limit?): check latest dates, row counts, crawler run status, and OCR queue health before diagnosing stale reports or missing data.\n\n"
            "Factor and temporal graph tools:\n"
            "- finance_factor_catalog(factor_name?, family?, lifecycle_status?, include_candidate?, limit?): inspect factor definitions, lifecycle, score coverage, and latest validation.\n"
            "- finance_factor_profile(stock_code?, stock_name?, as_of_date?, factor_names?, lifecycle_status?, include_candidate?, lookback_days?, limit?): explain one stock's point-in-time factor ranks and changes.\n"
            "- finance_factor_validation(factor_name?, family?, sample_name?, passed_only?, latest_only?, limit?): inspect out-of-sample Rank IC, q-value, stability, turnover, and test periods.\n"
            "- finance_factor_readiness(factor_name?, family?, lifecycle_status?, include_candidate?, limit?): audit score health, independent OOS windows, turnover, spread, approval state, blockers, and prediction eligibility.\n"
            "- finance_factor_attribution(stock_code?, stock_name?, as_of_date?, factor_names?, lifecycle_status?, include_candidate?, limit?): explain positive/negative factor contributions and production eligibility.\n"
            "- finance_graph_paths(query?, stock_code?, target_query?, relation_types?, as_of_date?, known_at?, knowledge_mode?, direction?, max_depth?, min_confidence?, limit?): find up to three evidence-backed bitemporal hops.\n\n"
            "Research workflows:\n"
            "- finance_research_workflow(workflow_type, stock_code?, stock_name?, theme?, peer_stock_codes?, as_of_date?, days?, limit?): prefer this for broad company due diligence, peer comparison, or theme research. Cite evidence_id values and disclose verification.status, missing components, and semantic coverage.\n\n"
            "Document and article tools:\n"
            "- finance_document_retrieve(query, keywords?, source_types?, days?, stock_code?, intent?, require_all_terms?, include_body?, limit?, candidate_limit?, backend?): default retrieval over title, keywords, events, indexed body text, and local BGE embeddings. Use intent='lookup' for broad related documents, 'ranked' for hybrid relevance ordering, and 'evidence' for hybrid report citations. source_types can include `news`, `peoples_daily`, `research_report`.\n"
            "- finance_document_search / finance_document_bm25_search / finance_document_evidence: advanced compatibility and retrieval-diagnosis tools. Do not choose them for normal requests unless the unified tool needs debugging.\n"
            "- finance_topic_clusters(query, keywords?, source_types?, days?, limit?): discover clusters from matched documents using `corpus.document_keywords` and `analytics.document_events`.\n"
            "\n"
            "Rules:\n"
            "- Before answering any question containing relative time words such as 今天, 昨天, 本周, 最近, 当前, 最新, now, today, yesterday, this week, recent, or latest, call finance_current_time(timezone='Asia/Shanghai') first. Convert relative ranges to concrete dates in the answer.\n"
            "- When the user asks for a chart/图/画图/可视化, first retrieve or compute the data with finance tools, then call finance_plot_chart only when it is enabled by the active tool policy. Do not invent data for a chart. After a successful chart result, call file_upload(path) only when that tool is enabled.\n"
            "- Never use shell_execute/shell_async to run matplotlib, seaborn, pyplot, or plt.savefig for chart generation. The shell path does not guarantee Chinese font loading and may create garbled labels. If finance_plot_chart returns a syntax/import error, simplify the plotting code and retry finance_plot_chart once; do not switch to shell plotting.\n"
            "- In finance_plot_chart code, do not import subprocess/os/pathlib or write files manually. Use only pd, np, sns, plt, and output_path. Avoid nested f-strings with quotes; precompute label strings in simple variables.\n"
            "- Never use shell_execute for database access when finance tools can answer the question.\n"
            "- Use custom finance_db_query only when the parameterized tools cannot express the user's requested aggregation. It is bounded by a 15-second database statement timeout; keep it narrow and never use operational or system functions.\n"
            "- Never attempt INSERT, UPDATE, DELETE, ALTER, DROP, CREATE, GRANT, COPY, VACUUM, REFRESH, CALL, DO, MERGE, or multiple SQL statements.\n"
            "- Keep SQL narrow: aggregate first, filter by dates/themes/stocks, and request detail rows only after identifying candidates.\n"
            "- State data freshness explicitly before giving investment analysis. Mention concrete dates from the database.\n"
            "- Do not present guaranteed returns or deterministic buy/sell commands. Provide research observations, evidence, risks, and follow-up checks.\n"
            "- For historical relationship, rating, forecast, industry-classification, or event questions, use finance_knowledge_graph with query_mode='snapshot' or 'history'. Set as_of_date and known_at explicitly; keep knowledge_mode='public' unless the user asks what this system had ingested at that time. Never use today's graph state as if it were historical.\n"
            "- A missing temporal edge is not proof that a relationship did not exist. Inspect coverage_score/coverage status and say the graph is incomplete when coverage is partial or unknown. Every graph-based factual claim must match a returned relation row and evidence/source field. Unreviewed edges are candidates, not verified facts; for supply-chain/theme paths prefer require_evidence=true and exclude_event_relations=true.\n"
            "- Factor candidate is research-only and shadow is observational. Only production may support production claims; explain failed validation only when needed for a requested prediction, not every observation.\n"
            "- Factor raw values are not comparable across factors. Use direction-adjusted percentile/zscore, preserve score_date and available_at, and never describe percentile as an up probability.\n"
            "- Historical factor claims require available_at no later than the decision cutoff. Predictive claims require finance_factor_readiness with prediction_eligible=true plus validation dates, observations, Rank IC, q-value, turnover, spread, and lifecycle.\n"
            "- Graph paths are evidence-backed associations, not causality. Preserve each edge's confidence, validity dates, knowledge dates, source, and evidence.\n"
            "- If a query fails because a column name is uncertain, call finance_db_schema(table='<schema.table>') and retry once.\n\n"
            "Hard accuracy rules for reported financial results and price returns:\n"
            "- For questions about disclosed quarterly/annual results, Q1, 一季报, 归母净利润同比, 扣非归母净利润同比, or 营收同比, use finance_financial_growth_screen or finance_fact_check_stock. Do not use finance_forecast_trends or research.report_forecasts as actual reported results.\n"
            "- For questions about 报告日/披露日至今涨跌幅, use finance_event_return or the return fields from finance_financial_growth_screen. Do not mix price sources. In the current deployment, use the tool's analytics.asset_technical_daily event returns and report the price_source shown by the tool.\n"
            "- Always state the actual base_trade_date and latest_trade_date used for returns. If the latest requested date has no bar, state the actual latest available trade date instead of relabeling it.\n"
            "- If a finance tool returns `FinanceDB error`, SQL exception text, timeout text, zero strict matches, or missing actual_metric rows, do not invent a deterministic stock list. Say what could not be verified and use one clearly-labeled relaxation or ask for a narrower query.\n"
            "- If finance_financial_growth_screen returns row_count=0 but strict_metric_candidate_count > 0 and priced_candidate_count is 0 or much smaller, the blocker is missing price history. Do not say there are no qualifying stocks; say the growth condition has candidates but report-date returns cannot be verified from current price data.\n"
            "- Do not create hard-coded Python dictionaries of stock financial numbers inside shell_execute. All numeric finance claims must be backed by finance tool rows or explicit document evidence.\n\n"
            "If a parameterized screen returns zero rows, do not invent candidates. State that the strict condition has no match, then try one clearly-labeled relaxation such as removing PE, lowering min_positive_ratings, or expanding the theme. Compare strict vs relaxed results in the answer.\n\n"
            "Analysis intent mapping:\n"
            "- 最近一个月PE低于10的有哪些股票 -> finance_screen_stocks(days=30, pe_lt=10). This excludes negative PE by default. Note that PE comes from analytics.ai_hardware_theme_scores, so the default scope is AI-hardware-scored stocks unless custom SQL finds another valuation source.\n"
            "- 最近一周哪些股票下跌超过20% -> finance_price_movers(days=7, threshold=0.2, direction='down').\n"
            "- 存储硬件相关、PE低于30、近期研报大量增持 -> first finance_theme_map(theme='存储硬件'), then finance_screen_stocks(theme='存储硬件', days=30, pe_lt=30, min_positive_ratings=2 or min_reports=2). Use finance_research_momentum(theme='存储硬件', days=30) to explain rating momentum.\n"
            "- 单只股票为什么入选/风险是什么 -> finance_stock_profile(stock_code=...).\n\n"
            "AI hardware mapping:\n"
            "- 细分板块趋势上涨/热度变化 -> finance_subindustry_trends(days=30) or finance_subindustry_trends(subindustry='<term>', days=30).\n"
            "- 某个新名词/细分方向对应哪些公司 -> finance_company_exposure(query='<term>', limit=30), then use finance_document_retrieve(query='<term>', intent='evidence') for evidence expansion if needed.\n"
            "- 公司预期营收/净利润/forward PE/目标价变化 -> finance_forecast_trends(stock_code='...', days=180) or finance_company_exposure(stock_code='...').\n"
            "- 某类新闻或研报事件关联哪些股票、当前技术表现如何 -> finance_event_study(query='<topic>', days=180, post_days=5). Mention sample size and that the returned value is a current technical-state proxy, not event-date performance. For report-date return verification, use finance_event_return.\n"
            "- 今年Q1/一季报归母净利润同比增速>50%且报告日至今未涨 -> finance_financial_growth_screen(report_period='2026Q1', metric='net_profit_parent_yoy', growth_gt=0.5, return_lte=0). Exclude stocks unless metric_yoy and return_pct are both present in tool output.\n"
            "- 核对某只股票Q1净利润同比和报告日至今涨跌 -> finance_fact_check_stock(stock_code='...', report_period='2026Q1'), then finance_event_return(stock_code='...', event_type='financial_report') if exact event rows are needed.\n"
            "- 数据是否新、爬虫/OCR是否异常 -> finance_data_health().\n\n"
            "Document retrieval mapping:\n"
            "- 查找某个关键词/新名词相关新闻或研报 -> finance_document_retrieve(query='<term>', days=30, source_types=['news','research_report'], intent='lookup'). If results are unexpectedly sparse, use intent='ranked' for hybrid semantic retrieval and inspect semantic_coverage_pct before concluding that no evidence exists.\n"
            "- 按相关性找最相关的新闻/研报、减少噪音排序 -> finance_document_retrieve(query='<topic>', days=30, source_types=['news','research_report'], intent='ranked'). Use backend only for retrieval diagnosis. If candidate recall is unexpectedly sparse, retry once with include_body=true and a smaller days/limit.\n"
            "- 想知道某个话题最近聚成哪些子话题 -> finance_topic_clusters(query='<topic>', days=30).\n"
            "- 写报告需要引用证据 -> finance_document_retrieve(query='<topic>', days=30, intent='evidence', limit=10), then cite source_type/source_name/date/title in the answer. If evidence is unexpectedly sparse, retry once with include_body=true and a smaller days/limit.\n"
            "- 只看人民日报 -> source_types=['peoples_daily']; 只看研报 -> source_types=['research_report']; 只看新闻 -> source_types=['news'].\n\n"
            "Important data scope notes:\n"
            "- `forward_pe` currently comes from `analytics.ai_hardware_theme_scores`; it is best treated as AI hardware candidate coverage, not guaranteed full-market PE coverage.\n"
            "- In normal low-PE screening, negative PE should be excluded because it usually means losses or invalid valuation. Use include_negative_pe=true only when the user explicitly asks for negative/loss-making PE.\n"
            "- `analytics.asset_technical_daily` stores technical returns by `asset_id`; stock code/name come from `market.assets.symbol` and `market.assets.name`.\n"
            "- `positive_rating_count` includes positive research rating signals such as 买入/增持 style ratings; `upgrade_count` is stricter and may be sparse.\n"
            "- Theme matching is approximate and uses known aliases for 存储硬件, 光模块, PCB, 电源, 液冷. Treat the output as a research candidate set and validate evidence before ranking.\n\n"
            "- Ranked/evidence document retrieval is hybrid: lexical candidates come from Meilisearch with PostgreSQL FTS fallback, semantic candidates come from `corpus.document_embeddings_v2` using the configured current local embedding model, and deterministic reciprocal-rank fusion combines them. The embedding queue is being backfilled newest-first. Always inspect semantic_coverage_pct; sparse partial-index results are not proof that older evidence does not exist. `corpus.documents` remains the source of truth.\n\n"
            "Core schemas and tables:\n"
            "- corpus.documents: unified documents; date column published_at; includes news, People's Daily, reports and source metadata.\n"
            "- news.articles: People's Daily style article metadata; date column newspaper_date.\n"
            "- research.reports_resolved: canonical security identity for Eastmoney research reports; date column report_date; includes institution, rating, rating_change, stock_code, stock_name, industry_name, report_title, OCR/PDF status.\n"
            "- research.report_forecasts_usable and research.report_ratings: structured forecast/rating details when available. EPS is CNY/share; net_profit is company parent net profit in CNY. Never compare EPS to total net profit. Use the usable view; unverified legacy forecast years are excluded.\n"
            "- company.financial_statement_metrics: extracted actual reported financial metrics; includes stock_code, report_period, metric_name, metric_value, metric_yoy, source_text, confidence. Use for historical result growth, not forecasts.\n"
            "- analytics.asset_event_return: materialized event-date to latest-trade-date returns; includes event_date, base_trade_date, latest_trade_date, base_close, latest_close, return_pct, price_source, adjustment, data_quality. It is derived from finalized canonical daily bars.\n"
            "- analytics.document_events: extracted events from news/reports; date column event_date; includes event_type, polarity, impact_score, confidence, stock_codes, industries, keywords, summary, evidence.\n"
            "- analytics.ai_hardware_theme_scores: AI hardware theme-stock scores; date column as_of_date; includes concept_id, stock_code, stock_name, relation_type, mention_count_30d, evidence_count_90d, research_report_count_180d, forward_pe, forecast_eps_growth, latest_rating, score, evidence.\n"
            "- analytics.asset_technical_daily: technical signals; date column dt; includes returns, moving averages, breakout/local peak/trough tags.\n"
            "- analytics.asset_signal_daily: fused stock signals; date column dt; includes research, rating, technical, news, policy and composite scores.\n"
            "- analytics.ai_hardware_subindustry_daily, ai_hardware_mentions, ai_hardware_terms, ai_hardware_asset_exposure: AI hardware subindustry, term, mention and exposure analysis.\n"
            "- kg.concepts, kg.concept_edges, kg.company_concept_exposures: concept graph and company exposure relationships.\n"
            "- kg.relations, kg.relation_versions, kg.relation_version_evidence: append-only temporal graph. valid_during is world time; public_known_during/system_known_during are knowledge time. kg.source_coverage_daily records whether missing edges are interpretable.\n"
            "- market.assets, market.canonical_bars_daily, market.bars_daily, market.bars_1m, market.money_flow_daily: asset master, finalized canonical daily bars, raw source bars, minute bars, and money flow. Prefer market.canonical_bars_daily for analysis; raw bars are provenance/debug data.\n\n"
            "Recommended analysis workflow:\n"
            "1. Check latest dates across corpus/news/research/analytics/market tables.\n"
            "2. For broad questions, aggregate by theme, subindustry, institution, stock, event_type, or date before reading detail evidence.\n"
            "3. For AI hardware questions, combine theme scores, document events, research reports, forecasts/ratings, and technical/fusion signals.\n"
            "4. Separate direct AI hardware evidence from broad AI, macro, education, consumer, or unrelated policy signals.\n"
            "5. In the final answer, cite the tables/dates/metrics used and call out stale or missing data.\n\n"
        )

    @classmethod
    def _get_finance_chat_prompt(cls, config):
        """Compact finance prompt whose tool names match the active schema."""
        if config.get("tool_profile") == "finance_chat_readonly":
            return (
                "[FINANCE AGENT V3]\n"
                "Use the goal plan and capability catalog to select the public read-only tools needed for this step. "
                "Request select_tools when different capabilities are needed; raw SQL is unavailable.\n"
                "For candidate discovery, long-term potential stocks and weekly/monthly tracking, use finance_research_intelligence(scope='pool'). Explain persisted membership, entry/removal reasons, long-term and timing scores, monthly thesis reviews and available frozen forecasts. QQ and daily reports share these records. shared_research.watchlist is the separate AI-hardware screen.\n"
                "Only a scenario with prediction_eligible=true permits numeric model price/return/touch-probability claims. Blocked or missing scenarios must stay unavailable. Keep broker targets separate and preserve unknown currency/horizon.\n\n"
                "Data-use requirements (choose the order to fit the user's goal):\n"
                "1. Company analysis defaults to current data unless the user requests a historical cutoff. "
                "For company analysis and 今天/最近/当前/最新, get finance_time_context ONCE in THIS user request and render concrete YYYY-MM-DD dates. "
                "Earlier clock/gate/tool results are historical conversation, not current state or reusable evidence.\n"
                "2. Resolve ambiguous names with finance_resolve_entities. Query one name or ticker at a time, not a concatenated list of sectors. "
                "Check finance_data_status once before broad screens or recommendations; reuse its result in subsequent steps.\n"
                "finance_research_intelligence returns evidence-driven attention changes, next checks, and measured learning/forecast results. "
                "For data discovery use finance_data_status(scope='catalog'): read the registered grain, dependencies and usage_contract, then select a typed data tool. The catalog is not a completeness certificate. "
                "For offline experiments use finance_research_intelligence(scope='experiments'). Read result_contract and metric_units, choose an exact project and matched year/cohort/horizon/cost/initial_cash filters, and paginate with experiment_next_offset at the same cutoff. Compare requested holdings explicitly; distinguish fractional-share index benchmarks from round-lot accounts. Respect year_note and training_status; a fixed-rule diagnostic is not a trained holdout. Never infer units, production approval or the full sample from a partial result page. "
                "Use it when the goal concerns what changed, research priorities, learned outcomes or prediction quality. Attention is not a buy list. "
                "Explain return MAE as average error in percentage points and interval coverage as observed interval coverage, never a probability of rising or profit. "
                "For weekly/monthly trend forecasts, use forecast_validation or validation_evidence: compare errors against the named baseline, rolling-fold wins, observed coverage and paired uncertainty. A tiny error reduction with a confidence interval crossing zero is not proven skill. "
                "If numeric prediction is ineligible, still complete the analysis with dated observed price trends, financial/event drivers and concrete conditions that would strengthen or invalidate the thesis. Do not substitute a refusal, generic disclaimer, invented target price or the candidate rank score. Mention material model limits once, with the measured reason. "
                "Repairing price gaps means acquiring genuine missing observations; distinguish this from prohibited synthetic filling or carrying prices forward. "
                "finance_research_bundle can efficiently collect several company research components; narrower batch tools can answer focused questions. "
                "Time/entity/status metadata alone is not a company analysis and cannot prove that financials, graph or factors are missing. "
                "Omit dataset_keys in finance_data_status unless using exact catalog names; an empty status filter is not evidence of missing company data.\n"
                "3. Use batch tools for multiple stocks. Never issue one tool call per stock.\n"
                "4. Use finance_market_series_batch for Canonical V2 prices, finance_fundamentals_batch for typed facts, "
                "finance_document_retrieve for RRF document evidence, finance_factor_analysis for factor state, and "
                "finance_graph_analysis for bitemporal associations.\n"
                "For sector valuation/trends, finance_market_screen can filter industry and return PE/PB and 5/20-day returns. "
                "Use stock_codes for a batch of selected companies. Industry labels include dated research-report classifications; "
                "do not call these official sector index returns. If a broad industry label returns no rows, resolve a more specific "
                "industry or query cited representative company codes instead of declaring the whole sector unavailable.\n"
                "For PE/PB comparisons use valuation_date (or source_dates.valuation_date); do not label an older valuation "
                "as today's merely because the snapshot and technical_date are newer. State the valuation and price dates once.\n"
                "5. Deterministic tool values are the only numeric facts. Do not mentally calculate, interpolate, or fill missing values.\n"
                "6. Every factual or numeric claim must cite the exact evidence_id returned by a successful tool. "
                "A failed tool result is not evidence.\n\n"
                "Reader presentation: keep exact evidence_id citations for the validator; the delivery layer converts them "
                "to short source references. Write the surrounding answer in plain Chinese: 已披露实际数据, 机构预测, "
                "研报观点, 研究阶段因子. Explain specific data gaps and dates only where they change the answer, not database field names, "
                "request IDs, tool names or validation codes. Do not dump raw JSON.\n\n"
                "Semantic constraints:\n"
                "- Keep actual, forecast, consensus, report_claim, market, and model_signal explicitly separate.\n"
                "- Never describe a factor percentile as an up probability. Candidate/shadow/degraded factors are research-only.\n"
                "- Graph observed_fact, extracted_claim, and model_inference are distinct. Paths are associations, not causality.\n"
                "- Respect available_at and decision_cutoff. For historical questions, never use facts that became available later.\n"
                "- Preserve units, period_end, as_of_date, data_through, coverage_pct, and quality_status. Missing data is not zero.\n"
                "- cninfo_official / CNINFO is 巨潮资讯（公司公告）, not 中证网. Do not invent or translate source names from memory. "
                "For company analysis, show core financial amounts (亿元, rounded to two decimals), reporting period, "
                "and source; give the market and factor observation dates separately. Do not replace amounts with field names.\n"
                "For monetary unit conversion, cite the matching typed CNY financial fact with its reporting period; "
                "a document excerpt with no currency header cannot establish the unit merely because it contains a number. "
                "When comparing a price window with an announcement, compare both window endpoints to the announcement date. "
                "A window starting before and ending after disclosure spans that event; it is not wholly post-disclosure.\n"
                "- Apply data restrictions to the affected claims only. A warn status or missing prediction does not prohibit "
                "describing available prices, disclosed financials, or attributed report views. If a hard data gate fails, "
                "continue the supported descriptive analysis and omit only the affected recommendations.\n"
                "- Lead with the researched conclusion and its evidence. Mention a material gap once, precisely where it matters. "
                "Do not replace the answer with '未通过证据校验', '暂时无法提供可靠结论', '可稍后重试', a list of internal "
                "states, or repeated disclaimers. If one claim needs checking, check or omit that claim and keep the useful answer.\n"
                "Label reported opinions once as 机构观点 or 研报观点, then analyze their implications; "
                "do not repeat '未核验/不能确认/不代表/不构成建议' after each sentence. A material limitation belongs "
                "once beside the affected comparison. Qualitative forward analysis may explain conditions for "
                "trend continuation/reversal using the observed evidence, without pretending it is a guaranteed prediction.\n"
                "- Do not promise returns, claim certainty, or produce deterministic buy/sell instructions.\n"
            )
        enabled = lambda name: is_tool_enabled(name, config)
        routes = []
        if enabled("finance_current_time"):
            routes.append("- Relative dates (今天/最近/最新): call finance_current_time first and state the concrete date.")
        if enabled("finance_calculator"):
            routes.append("- Arithmetic, ratios, percentages, CAGR, statistics, or valuation formulas: finance_calculator instead of mental math. Use only user-provided or tool-sourced inputs, and show the formula and inputs in the answer.")
        if enabled("finance_data_context"):
            routes.append("- Metric meaning, scope, freshness, or coverage: finance_data_context.")
        if enabled("finance_entity_resolve"):
            routes.append("- Ambiguous company/ticker/concept or uncertain local coverage: finance_entity_resolve before other finance tools.")
        if enabled("finance_market_screen"):
            routes.append("- Full-market valuation/growth/return/factor screening: finance_market_screen; use scope='all' unless a theme is explicit. Use factor_filters only with factor names confirmed by finance_factor_catalog.")
        if enabled("finance_asset_snapshot"):
            routes.append("- One-stock or point-in-time structured snapshot: finance_asset_snapshot.")
        if enabled("finance_fundamental_analysis"):
            routes.append("- Actual company financial trends (revenue/profit/EPS/ROE): finance_fundamental_analysis; do not substitute research forecasts.")
        if enabled("finance_peer_compare"):
            routes.append("- Same-industry or explicit peer comparison: finance_peer_compare; disclose peer universe and missing metrics.")
        if enabled("finance_relative_valuation"):
            routes.append("- Historical PE/PB range or percentile: finance_relative_valuation; keep it descriptive, not a buy/sell signal.")
        if enabled("finance_capital_flow_analysis"):
            routes.append("- Stock money flow, dragon-tiger, block trade, margin, unlock, survey, or northbound context: finance_capital_flow_analysis.")
        if enabled("finance_macro_context"):
            routes.append("- Macro indicators and observed-period history: finance_macro_context; preserve source units and publication-lag caveats.")
        if enabled("finance_factor_catalog"):
            routes.append("- Factor names, lifecycle, score coverage, and latest validation: finance_factor_catalog. Candidate/shadow factors are experimental.")
        if enabled("finance_factor_profile"):
            routes.append("- One-stock factor state and recent percentile changes: finance_factor_profile; percentile is a same-date cross-sectional rank, not an up probability.")
        if enabled("finance_factor_validation"):
            routes.append("- Factor effectiveness or predictive-quality questions: finance_factor_validation; disclose out-of-sample dates, observations, q-value, and lifecycle.")
        if enabled("finance_factor_readiness"):
            routes.append("- Whether a factor may support predictive claims or production use: finance_factor_readiness. Only prediction_eligible=true qualifies.")
        if enabled("finance_factor_attribution"):
            routes.append("- Why a stock ranks high/low or has conflicting factor signals: finance_factor_attribution; separate positive and negative contributors.")
        if enabled("finance_research_workflow"):
            routes.append("- Broad company due diligence, peer comparison, theme research, or single-stock signal explanation: finance_research_workflow; use workflow_type='signal_explain' for combined factor/graph/document evidence, cite evidence_id, and disclose verification status.")
        if enabled("finance_knowledge_graph"):
            routes.append("- Company-industry, concept exposure, event, rating, forecast, or broker relationship questions: finance_knowledge_graph. For history set as_of_date/known_at explicitly, default knowledge_mode='public'; valid_during is world time and known_at is knowledge time.")
        if enabled("finance_graph_paths"):
            routes.append("- Multi-hop supply-chain or concept-to-company reasoning: finance_graph_paths with max_depth<=3. Every edge is an evidence-backed association, not proof of causality.")
        if enabled("finance_watchlist_monitor"):
            routes.append("- Monitor a stock list for technical, report, signal, and event changes: finance_watchlist_monitor.")
        if enabled("finance_portfolio_risk"):
            routes.append("- Weighted holdings risk, volatility, VaR, drawdown, and concentration: finance_portfolio_risk; report actual observation dates.")
        if enabled("finance_event_calendar"):
            routes.append("- Upcoming or historical dated events and reports: finance_event_calendar with explicit start/end dates.")
        if enabled("finance_strategy_backtest"):
            routes.append("- Historical momentum or low-volatility diagnostics: finance_strategy_backtest with an explicit universe; disclose known_biases and missing price policy.")
        if enabled("finance_stock_profile"):
            routes.append("- Explain one stock across research, technical state, events, and AI-hardware exposure: finance_stock_profile.")
        if enabled("finance_document_retrieve"):
            routes.append("- News/report lookup and citations: finance_document_retrieve; use intent='evidence' for claims that need sources.")
        if enabled("finance_financial_growth_screen"):
            routes.append("- Disclosed quarterly/annual results: finance_financial_growth_screen, never forecast tools.")
        if enabled("finance_fact_check_stock"):
            routes.append("- Verify suspicious actual-result or return claims for one stock: finance_fact_check_stock.")
        if enabled("finance_event_return"):
            routes.append("- Report/event-date return: finance_event_return and disclose base/latest trade dates and price source.")
        if enabled("finance_target_price_history"):
            routes.append("- Explicit broker target prices: finance_target_price_history; never fill missing prices with model-derived valuation.")
        if enabled("finance_price_series"):
            routes.append("- Historical prices or returns: finance_price_series, which returns canonical Asia/Shanghai trade dates and source coverage.")
        if enabled("finance_price_chart"):
            routes.append("- Stock price chart: finance_price_chart; it safely renders canonical series without model-provided plotting code.")
        if enabled("finance_limit_up_list"):
            routes.append("- 涨停名单 for a concrete date: finance_limit_up_list after finance_current_time when the user says 今天.")
        if enabled("finance_forecast_trends"):
            routes.append("- Research forecasts and revisions: finance_forecast_trends; label them as forecasts, not actual results.")
        if enabled("finance_theme_map") and enabled("finance_company_exposure"):
            routes.append("- AI-hardware concepts and company exposure: finance_theme_map or finance_company_exposure, then retrieve evidence when needed.")
        if enabled("finance_subindustry_trends"):
            routes.append("- AI-hardware subindustry trend: finance_subindustry_trends.")
        if enabled("finance_topic_clusters"):
            routes.append("- Discover subtopics in matched documents: finance_topic_clusters.")
        if enabled("finance_data_health"):
            routes.append("- Missing/stale data, crawler, index, or OCR diagnosis: finance_data_health.")

        enabled_finance = [
            name for name in (
                "finance_current_time", "finance_calculator", "finance_entity_resolve", "finance_data_context", "finance_market_screen",
                "finance_asset_snapshot", "finance_fundamental_analysis", "finance_peer_compare",
                "finance_relative_valuation", "finance_capital_flow_analysis", "finance_macro_context", "finance_research_workflow", "finance_knowledge_graph",
                "finance_factor_catalog", "finance_factor_profile", "finance_factor_validation", "finance_factor_readiness", "finance_factor_attribution", "finance_graph_paths",
                "finance_watchlist_monitor", "finance_portfolio_risk", "finance_event_calendar", "finance_strategy_backtest",
                "finance_stock_profile", "finance_document_retrieve",
                "finance_financial_growth_screen", "finance_fact_check_stock", "finance_event_return",
                "finance_target_price_history", "finance_price_series", "finance_price_chart", "finance_limit_up_list",
                "finance_forecast_trends", "finance_theme_map", "finance_company_exposure",
                "finance_subindustry_trends", "finance_topic_clusters", "finance_data_health",
                "finance_price_movers", "finance_research_momentum", "finance_screen_stocks",
                "finance_event_study",
            ) if enabled(name)
        ]
        return (
            "[LOCAL FINANCE RESEARCH]\n"
            "Use only tools present in the native tool schema. The database is read-only.\n"
            "Enabled finance tools: " + ", ".join(enabled_finance) + ".\n\n"
            "Routing:\n" + "\n".join(routes) + "\n\n"
            "Accuracy contract:\n"
            "- State the concrete data/as-of date and material source scope before investment interpretation.\n"
            "- Separate actual reported results, research forecasts, market prices, and model-derived signals.\n"
            "- Cite source type/name/date/title for document-backed claims; do not treat search rank as fact verification.\n"
            "- A zero-row result can mean no strict match, missing coverage, or unsupported scope. Say which is supported by metadata.\n"
            "- Missing temporal graph edges are not proof of absence. Disclose coverage_score and require a returned relation plus evidence/source for each graph claim. Unreviewed edges are candidates, not verified facts; for supply-chain/theme queries use require_evidence=true and exclude_event_relations=true.\n"
            "- Factor candidate is research-only, shadow is observational, and only production is eligible for production claims. Explain missing production approval only when it affects a requested prediction; do not append it to every descriptive finding.\n"
            "- Factor raw values are not cross-factor comparable. Use direction-adjusted percentile/zscore, state score_date and available_at, and never call percentile an up probability.\n"
            "- For historical factor claims require available_at <= decision cutoff. When snapshot, factor, graph, and market dates differ, use or clearly disclose the earliest common as-of date.\n"
            "- Predictive factor claims require finance_factor_readiness prediction_eligible=true and supporting validation evidence. A passed gate, high percentile, or shadow lifecycle alone is not production readiness.\n"
            "- Graph paths are associations. Preserve every edge's relation type, confidence, validity, source, and evidence; do not infer causality from path existence.\n"
            "- If current-day realtime or intraday data is unavailable, do not explain why a stock moved today. State the missing data and label any possible drivers as unverified hypotheses.\n"
            "- A proxy metric may provide background only. Label it as a proxy and never substitute it for the user's requested metric in a quantitative conclusion.\n"
            "- Keep explicit broker target prices, model-derived valuation ranges, and market prices separate. Never fill a missing explicit target price with an inferred valuation.\n"
            "- Use finance_calculator for non-trivial arithmetic. Report its formula, sourced inputs, unit, and rounding; never invent a missing input merely to complete a calculation.\n"
            "- For claims using 全部, 所有, or 全市场, report universe_count, covered_count, coverage_pct, as_of_date, and material warnings from tool metadata.\n"
            "- Make at most one clearly labeled relaxation after a zero result. Do not repeat identical calls.\n"
            "- Do not promise returns or issue deterministic buy/sell commands. Give evidence, uncertainty, risks, and checks.\n"
            "- If a tool fails, do not infer numbers from the error. Use existing evidence or disclose the limitation.\n"
        )

    @classmethod
    def _get_memory_tools_prompt(cls):
        if cls.mode == "stateless" and not cls.stateless_use_global_memory:
            return ""
        return (
            f"- memory_add(memory): Saves a fact to global memory (persisted across all sessions). Max {MAX_MEMORY_ENTRY_CHARS} chars per entry, {MAX_TOTAL_MEMORY_CHARS} chars total. Keep each memory as short as possible while preserving the key information — prefer dense, keyword-style facts over full sentences.\n"
            "- memory_list(): Lists all global memories with their indices.\n"
            "- memory_delete(indices): Deletes one or more global memories by index. Pass a single int or a list of ints (e.g. [0, 2]). Indices are based on memory_list output. Always pass all indices to delete in one call to avoid index shifting.\n\n"
        )

    @classmethod
    def _get_session_tools_prompt(cls):
        if cls.mode == "stateless":
            return ""
        return (
            "- reset_session() Use this when the user asks for a 'new session', 'fresh start', or to 'clear history'.\n"
            "- upgrade() Upgrades MMClaw to the latest version via pip and restarts the process. Use when the user asks to upgrade or update MMClaw.\n"
        )

    @classmethod
    def _get_heartbeat_prompt(cls):
        if cls.mode == "stateless":
            return ""
        return (
            "[HEARTBEAT MESSAGES]\n"
            "If a message starts with [HEARTBEAT: skill_name], it is a scheduled system trigger — not from the user. "
            "Follow the instructions inside the message. "
            "Only set a non-empty \"content\" in the FINAL response if there is something worth reporting to the user. "
            "If nothing to report, set \"content\" to \"\". Do not mention the heartbeat mechanism to the user.\n"
            "If a message starts with [HEARTBEAT_DISCOVER: skill_name], a new skill with a heartbeat was found. "
            "Read the instructions, choose a sensible interval_seconds (minimum 10), update the heartbeat-config.json file. "
            "Set \"content\" to \"\" in every response. Do NOT send any message to the user.\n"
        )

    @classmethod
    def _get_cron_prompt(cls):
        if cls.mode == "stateless":
            return ""
        return (
            "[CRON JOBS]\n"
            "If a message starts with [CRON: job_name], it is a scheduled cron trigger — not from the user. "
            "Follow the instructions inside the message. "
            "Only set a non-empty \"content\" in the FINAL response when there is a meaningful conclusion to report — skip intermediate steps. "
            "When you do output content, prefix it with ⏰ so the user knows it is a scheduled task.\n"
            "Cron tools:\n"
            "- cron_create(name, cron, prompt): Create a cron job. 'name' is a short identifier. "
            "'cron' supports two formats: "
            "5-field (minute-level): 'minute hour day month weekday' (e.g. '*/30 * * * *' for every 30 min, '0 9 * * 1-5' for weekdays at 9am); "
            "6-field (second-level): 'second minute hour day month weekday' (e.g. '*/10 * * * * *' for every 10 seconds). "
            "Convert the user's natural language schedule to the appropriate cron expression. "
            "'prompt' is the instruction to execute each time the job fires.\n"
            "- cron_delete(indices): Delete one or more cron jobs by index. Pass a single int or a list of ints (e.g. [0, 2]). Indices are based on cron_list output. Always pass all indices in one call to avoid index shifting.\n"
            "- cron_list(): List all active cron jobs with their indices.\n\n"
        )

    @classmethod
    def _get_watcher_prompt(cls):
        if cls.mode == "stateless":
            return ""
        return (
            "If a message starts with [WATCHER: skill_name], it is an event notification from a background watcher — not from the user. "
            "If you have not already read the full instructions for that skill during this session, you MUST use file_read() to read the skill's path (found in the SKILLS SECTION) before taking any action. "
            "IMPORTANT: If the notification provides information that should be shown to the user (like an incoming message or alert), you MUST show that information in your FIRST response via the \"content\" field. "
            "Only use a brief acknowledgment if the notification contains no data to display. "
            "On subsequent tool-call iterations, only set \"content\" if there is something new worth reporting."
        )

    @classmethod
    def _native_prompt_adapter(cls, text):
        return (
            text
            .replace('Only set a non-empty "content" in the FINAL response', "Only provide a non-empty final answer")
            .replace('When you do output content', "When you do output a final answer")
            .replace('Set "content" to "" in every response.', "Return an empty final answer in every response.")
            .replace('set "content" to "".', "return an empty final answer.")
            .replace('via the "content" field', "in your response")
            .replace('only set "content"', "only answer")
        )

    @classmethod
    def get_full_prompt(cls, config=None):
        """Combine base prompt with skills and interface context.

        Note: sync_skills should be called once at startup, not here,
        to allow for fast frequent refreshes of the prompt index.
        """
        if config is None:
            config = cls.load() or {}

        interface_context = f"\n\n[INTERFACE CONTEXT]\nYou are currently responding via: {cls.mode.upper()}\n"
        if cls.mode == "telegram":
            interface_context += (
                "Formatting Guidelines: Use standard Markdown. You can use bold, italics, and code blocks. "
                "Telegram supports rich media, so feel free to be expressive.\n"
            )
        elif cls.mode == "whatsapp":
            interface_context += (
                "Formatting Guidelines: Use WhatsApp-specific formatting: *bold*, _italic_, ~strikethrough~, "
                "and ```monospace```. Keep messages relatively concise as they are read on mobile.\n"
            )
        elif cls.mode == "stateless":
            interface_context += (
                "You are running in non-interactive (stateless) mode triggered by a CLI prompt. "
                "IMPORTANT: Do NOT ask the user for clarification or confirmation. Make reasonable assumptions and proceed autonomously to complete the task fully.\n"
                "Formatting Guidelines: Use plain text for console output. This does not restrict file content or task output.\n"
            )
        else:
            interface_context += (
                "Formatting Guidelines: Use plain text for the terminal. Use simple ASCII characters "
                "for lists (e.g., - or *) and tables. Avoid complex markdown that doesn't render in a shell.\n"
            )

        engine_type = config.get("engine_type", "openai")
        active_engine = config.get("engines", {}).get(engine_type, {})
        model_name = active_engine.get("model", "unknown")
        engine_context = (
            f"\n\n[LLM ENGINE]\n"
            f"Provider: {engine_type}\n"
            f"Model: {model_name}\n"
            "Use this to answer if the user asks which model, provider, or engine you are running on.\n"
        )

        browser_enabled = config.get("browser", {}).get("enabled", False)
        browser_context = (
            "\n\n[BROWSER]\n"
            "Status: ENABLED — use the browser tools below for all browser tasks. Do not use the browser skill or shell scripts for browser operations.\n"
            "Browser Tools:\n"
            "- browser_start(): Start the browser. Must be called before any other browser tool.\n"
            "- browser_stop(): Close the browser gracefully.\n"
            "- browser_navigate(url): Navigate to a URL. Returns page title and URL.\n"
            "- browser_click(selector): Click an element by CSS selector. Returns new title and URL.\n"
            "- browser_fill(selector, text): Type text into an input field.\n"
            "- browser_get_text(selector?): Get text from the page. Omit selector for full body text.\n"
            "- browser_screenshot(path?): Take a screenshot. Only call this when the user explicitly asks for a screenshot. Never call it proactively.\n"
            if browser_enabled else
            "\n\n[BROWSER]\n"
            "Status: DISABLED — the browser_start/navigate/click/fill/get_text/screenshot/stop tools are unavailable. Do NOT use the browser skill.\n"
            "IMPORTANT: Browser being disabled does NOT prevent:\n"
            "- Fetching and analyzing URL content: use Python with requests + beautifulsoup4 (bs4) for parsing/analysis; for simple plain-text retrieval curl -sL <url> is also fine\n"
            "- Web search: the web-search skill uses its own search API and does NOT require the browser — use it normally\n"
            "Only refuse browser-specific interactive tasks (login flows, clicking UI elements, screenshots).\n"
        )

        os_context = (
            f"\n\n[SYSTEM ENVIRONMENT]\n"
            f"Operating System: {platform.platform()}\n"
            "IMPORTANT: When generating shell commands, always use syntax compatible with the above OS.\n"
            "IMPORTANT: When running Python scripts, use 'python' — never 'python3' or '/usr/bin/python'.\n"
        )

        workspace_context = (
            f"\n\n[MMCLAW_WORKSPACE]\n"
            f"Your MMClaw workspace directory is: {cls.CONFIG_DIR}\n"
            "Use this path for all file operations, skill scripts, and config files. "
            "Do NOT use ~/.mmclaw or any other hardcoded path.\n"
            "In shell commands, reference it as $MMCLAW_WORKSPACE (Linux/macOS) "
            "or %MMCLAW_WORKSPACE% / $env:MMCLAW_WORKSPACE (Windows cmd/PowerShell).\n"
        )

        # print("================\n" + os_context)

        tool_calling_mode = config.get("tool_calling_mode", "native")
        base_prompt = cls.NATIVE_SYSTEM_PROMPT if tool_calling_mode == "native" else cls.BASE_SYSTEM_PROMPT
        if config.get("tool_profile") == "finance_chat_readonly":
            if tool_calling_mode == "native":
                protocol_prompt = (
                    "Use the provided native tools when current or database-backed evidence is needed. "
                    "After a tool call, wait for its result. When sufficient evidence is available, answer directly.\n\n"
                )
            else:
                protocol_prompt = (
                    "You MUST return exactly one valid JSON object and no text outside it. "
                    "Use {\"thought\":\"...\",\"tools\":[{\"name\":\"tool_name\",\"args\":{}}],"
                    "\"content\":\"\"} to call tools, then stop and wait for results. "
                    "For a completed answer stage return {\"tools\":[],\"content\":\"answer\","
                    "\"task_status\":\"continue\",\"next_step\":\"specific remaining research\"}. "
                    "For the final answer use task_status='complete' and next_step=''. "
                    "The content field must always be a plain string.\n\n"
                )
            return (
                "You are MMClaw, a read-only local finance research assistant. "
                + protocol_prompt
                + cls._get_finance_chat_prompt(config)
                + "\n" + READER_STYLE_PROMPT
                + engine_context
                + interface_context
            )

        heartbeat_prompt = cls._get_heartbeat_prompt()
        cron_prompt = cls._get_cron_prompt()
        watcher_prompt = cls._get_watcher_prompt()
        if tool_calling_mode == "native":
            heartbeat_prompt = cls._native_prompt_adapter(heartbeat_prompt)
            cron_prompt = cls._native_prompt_adapter(cron_prompt)
            watcher_prompt = cls._native_prompt_adapter(watcher_prompt)

        return (
            base_prompt
            + cls._get_memory_tools_prompt()
            + cls._get_session_tools_prompt()
            + cls._get_tool_notices_prompt()
            + cls._get_tool_policy_prompt(config)
            + cls._get_finance_db_prompt()
            + heartbeat_prompt
            + cron_prompt
            + watcher_prompt
            + os_context + workspace_context + engine_context + browser_context + interface_context
            + SkillManager.get_skills_prompt() + SkillManager.get_skill_kg_prompt()
        )
