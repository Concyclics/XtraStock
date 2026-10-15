#!/usr/bin/env python3
"""Move MMClaw runtime secrets from workspace JSON into a chmod-600 env file."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


SECRET_MAPPINGS = (
    ("engines", "deepseek", "api_key", "api_key_env", "DEEPSEEK_API_KEY"),
    ("connectors", "qqbot", "app_secret", "app_secret_env", "MMCLAW_QQ_APP_SECRET"),
    ("connectors", "wechat", "token", "token_env", "MMCLAW_WECHAT_TOKEN"),
)


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip()] = value
    return lines, values


def write_env(path: Path, lines: list[str], values: dict[str, str], additions: dict[str, str]) -> None:
    replaced: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in additions:
                output.append(f"{key}={additions[key]}")
                replaced.add(key)
                continue
        output.append(line)
    for key, value in additions.items():
        if key not in replaced and key not in values:
            output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.finance-agent-v3")
    parser.add_argument(
        "configs", nargs="*",
        default=[
            "MMClaw/workspaces/qq_agent/mmclaw.json",
            "MMClaw/workspaces/wechat_agent/mmclaw.json",
        ],
    )
    args = parser.parse_args()
    env_path = Path(args.env_file).resolve()
    lines, existing = read_env(env_path)
    additions: dict[str, str] = {}

    for raw_path in args.configs:
        path = Path(raw_path).resolve()
        config = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        for section, name, value_key, env_key, env_name in SECRET_MAPPINGS:
            target = config.get(section, {}).get(name, {})
            value = str(target.get(value_key) or "").strip()
            if value:
                prior = additions.get(env_name) or existing.get(env_name)
                if prior and prior != value:
                    raise RuntimeError(f"conflicting secret values for {env_name}")
                additions[env_name] = value
                target[value_key] = None
                changed = True
            if target and target.get(env_key) != env_name:
                target[env_key] = env_name
                changed = True
        if changed:
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)

    if additions:
        write_env(env_path, lines, existing, additions)
    else:
        os.chmod(env_path, 0o600)
    print({"configs": len(args.configs), "migrated_variables": sorted(additions)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
