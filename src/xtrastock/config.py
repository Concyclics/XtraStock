"""One configuration boundary for collectors, SQL tools, agents and gateways.

Precedence: process environment > selected dotenv > local TOML > defaults.
Importing this module never connects to a database, starts a model or creates files.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping
import tomllib

from dotenv import dotenv_values


REPO_ROOT = Path(__file__).resolve().parents[2]

# Old entrypoints keep their established variable names. All deployment-specific
# defaults now come from config/defaults.toml, not per-script fallbacks.
ENV_KEYS = {
    "FINANCE_PGHOST": "database.host",
    "FINANCE_PGPORT": "database.port",
    "FINANCE_PGDATABASE": "database.name",
    "POSTGRES_DB": "database.name",
    "POSTGRES_USER": "database.admin_user",
    "POSTGRES_PASSWORD": "database.admin_password",
    "POSTGRES_APP_USER": "database.app_user",
    "APP_DB_PASSWORD": "database.app_password",
    "AI_READONLY_DB_USER": "database.reader_user",
    "AI_READONLY_DB_PASSWORD": "database.reader_password",
    "PGSSLMODE": "database.sslmode",
    "FINANCE_API_BASE_URL": "api.base_url",
    "FINANCE_API_KEY": "api.key",
    "FINANCE_AGENT_V3_MODEL": "api.model",
    "FINANCE_DAILY_MODEL": "daily.model",
    "FINANCE_DAILY_REASONING_EFFORT": "daily.reasoning_effort",
    "FINANCE_EMBEDDING_PROVIDER": "embedding.provider",
    "FINANCE_EMBEDDING_MODEL": "embedding.model",
    "FINANCE_EMBEDDING_DIMENSIONS": "embedding.dimensions",
    "FINANCE_EMBEDDING_BASE_URL": "embedding.base_url",
    "FINANCE_EMBEDDING_API_KEY_ENV": "embedding.key_env",
    "FINAGENT_GATEWAY_KEY": "gateway.key",
    "FINAGENT_OCR_BACKEND_URL": "gateway.ocr_backend_url",
    "FINAGENT_EMBEDDING_BACKEND_URL": "gateway.embedding_backend_url",
    "FINAGENT_MODEL_IDLE_SECONDS": "gateway.idle_seconds",
    "FINAGENT_REMOTE_POOL_CONFIG": "gateway.remote_pool_config",
    "FINANCE_OCR_BASE_URL": "ocr.base_url",
    "FINANCE_OCR_MODEL": "ocr.model",
    "FINANCE_OCR_API_KEY_ENV": "ocr.key_env",
    "FINAGENT_CLOUD_BASE_URL": "api.base_url",
    "FINAGENT_CLOUD_KEY": "api.key",
    "MEILI_URL": "search.url",
    "MEILI_INDEX": "search.index",
    "MEILI_MASTER_KEY": "search.key",
    "TZ": "project.timezone",
}


def _merge(left: dict, right: Mapping) -> dict:
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(left.get(key), dict):
            _merge(left[key], value)
        else:
            left[key] = value
    return left


def _set(tree: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        tree = tree.setdefault(part, {})
    tree[parts[-1]] = value


class Settings:
    def __init__(self, root: Path = REPO_ROOT, environ: Mapping[str, str] | None = None):
        self.root = Path(root).resolve()
        env = dict(os.environ if environ is None else environ)
        self.values: dict[str, Any] = {}
        with (self.root / "config/defaults.toml").open("rb") as stream:
            _merge(self.values, tomllib.load(stream))
        local = Path(env.get("XTRASTOCK_CONFIG", self.root / "config/local.toml")).expanduser()
        if not local.is_absolute():
            local = self.root / local
        if "XTRASTOCK_CONFIG" in env and not local.is_file():
            raise ValueError("XTRASTOCK_CONFIG does not name an existing file")
        if local.exists():
            with local.open("rb") as stream:
                _merge(self.values, tomllib.load(stream))
        dotenv = Path(env.get("XTRASTOCK_ENV_FILE", self.root / ".env")).expanduser()
        if not dotenv.is_absolute():
            dotenv = self.root / dotenv
        if "XTRASTOCK_ENV_FILE" in env and not dotenv.is_file():
            raise ValueError("XTRASTOCK_ENV_FILE does not name an existing file")
        # Do not expand dotenv references through ambient process variables.
        file_env = dotenv_values(dotenv, interpolate=False) if dotenv.exists() else {}
        self.environment = {k: str(v) for k, v in file_env.items() if v is not None}
        self.environment.update(env)
        for alias, canonical in (("PGHOST", "FINANCE_PGHOST"), ("PGPORT", "FINANCE_PGPORT")):
            if alias in env and canonical not in env:
                self.environment[canonical] = env[alias]
        for key, dotted in ENV_KEYS.items():
            if key in self.environment:
                _set(self.values, dotted, self.environment[key])
        port = int(self.get("database.port"))
        if not 1 <= port <= 65535:
            raise ValueError("database.port must be between 1 and 65535")
        if int(self.get("embedding.dimensions")) not in (512, 1024):
            raise ValueError("embedding.dimensions must match a supported SQL vector schema (512 or 1024)")

    def get(self, dotted: str, default: Any = None) -> Any:
        value: Any = self.values
        for part in dotted.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value

    def path(self, dotted: str) -> Path:
        path = Path(str(self.get(dotted))).expanduser()
        return path if path.is_absolute() else self.root / path

    def postgres_kwargs(self, role: str = "app") -> dict[str, Any]:
        if role not in {"app", "admin", "reader"}:
            raise ValueError("Unknown database role")
        user = self.get(f"database.{role}_user")
        password = self.get(f"database.{role}_password", "")
        # A read-only agent must never inherit the writer/admin override.
        if role != "reader":
            user = self.environment.get("FINANCE_PGUSER", user)
            password = self.environment.get("FINANCE_PGPASSWORD", password)
        if not password:
            raise ValueError(f"Missing {role} database password; configure .env or an external secrets file")
        return {
            "host": self.get("database.host"), "port": int(self.get("database.port")),
            "dbname": self.get("database.name"), "user": user, "password": password,
            "sslmode": self.get("database.sslmode"), "connect_timeout": 10,
        }

    def legacy_environment(self) -> dict[str, str]:
        result = dict(self.environment)
        for key, dotted in ENV_KEYS.items():
            value = self.get(dotted)
            if value is not None:
                result.setdefault(key, str(value))
        result.setdefault("FINANCE_EMBED_CACHE_DIR", str(self.path("paths.models") / "fastembed"))
        result.setdefault("LLM_BASE_URL", str(self.get("api.base_url")))
        result.setdefault("LLM_API_KEY", str(self.get("api.key", "")))
        result.setdefault("LLM_MODEL_FAST", str(self.get("api.model")))
        result.setdefault("LLM_MODEL_REASONING", str(self.get("api.model")))
        result.setdefault("FINANCE_ROOT", str(self.path("paths.app")))
        result.setdefault("FINANCE_CORPUS_ROOT", str(self.path("paths.app")))
        result.setdefault("XTRASTOCK_STATE_DIR", str(self.path("paths.state")))
        result.setdefault("XTRASTOCK_WORKSPACES_DIR", str(self.path("paths.workspaces")))
        return result


    def agent_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """Overlay an explicitly selected XtraStock engine in memory only."""
        import copy
        result = copy.deepcopy(config)
        if result.get("engine_type") == "openai_compatible_xtrastock":
            result.setdefault("engines", {})["openai_compatible_xtrastock"] = {
                "base_url": self.get("api.base_url"), "api_key": self.get("api.key", ""),
                "model": self.get("api.model"),
            }
        return result


def bootstrap_environment() -> None:
    """Compatibility bridge for direct script entrypoints; explicit env wins."""
    for key, value in Settings().legacy_environment().items():
        os.environ.setdefault(key, value)


def load_legacy_env(path: Path | None = None) -> dict[str, str]:
    """Read a specified legacy file, then overlay the centralized configuration."""
    values = dotenv_values(path, interpolate=False) if path and path.is_file() else {}
    return {**{k: str(v) for k, v in values.items() if v is not None}, **Settings().legacy_environment()}
