#!/usr/bin/env python3
"""Run one real JSON-protocol MMClaw tool loop against the configured gateway."""

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
from mmclaw.tool_schemas import get_native_tool_schemas  # noqa: E402
from mmclaw.tools import FinanceDBTool  # noqa: E402


def parse_json_object(text: str) -> dict:
    text = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model did not return a JSON object")
    return json.loads(text[start:end + 1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workspace",
        type=Path,
        default=ROOT / "MMClaw/workspaces/qq_agent",
    )
    args = parser.parse_args()

    config = json.loads((args.workspace / "mmclaw.json").read_text(encoding="utf-8"))
    ConfigManager.mode = "qq"
    ConfigManager.CONFIG_DIR = args.workspace
    user_text = "请调用时间工具，告诉我当前的北京时间和具体日期。"
    schemas = get_native_tool_schemas(config, context_text=user_text)
    contract = (
        "[TOOLS AVAILABLE THIS ROUND]\n"
        "Only call tools from these schemas. Put calls in the top-level tools array and stop:\n"
        + json.dumps(schemas, ensure_ascii=False, separators=(",", ":"))
    )
    messages = [
        {"role": "system", "content": ConfigManager.get_full_prompt(config)},
        {"role": "system", "content": contract},
        {"role": "user", "content": user_text},
    ]
    engine = Engine(config)
    first = engine.ask(messages)
    first_data = parse_json_object(first.get("content"))
    calls = first_data.get("tools") or []
    if not calls or calls[0].get("name") != "finance_time_context":
        raise RuntimeError(f"unexpected first tool call: {calls[:1]}")

    call = calls[0]
    result = FinanceDBTool.time_context(**(call.get("args") or {}))
    messages.extend([
        {"role": "assistant", "content": first.get("content") or ""},
        {"role": "user", "content": f"Tool Output (finance_time_context):\n{result}"},
        {
            "role": "system",
            "content": "[FINAL ANSWER REQUIRED]\nReturn tools=[] and put the final answer in content.",
        },
    ])
    second = engine.ask(messages)
    second_data = parse_json_object(second.get("content"))
    content = str(second_data.get("content") or "")
    if second_data.get("tools") or not re.search(r"20\d{2}-\d{2}-\d{2}", content):
        raise RuntimeError("final response was not a dated tool-grounded answer")
    print(json.dumps({
        "status": "ok",
        "provider": first.get("_mmclaw_provider"),
        "model": first.get("_mmclaw_model"),
        "first_tool": call.get("name"),
        "final_chars": len(content),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
