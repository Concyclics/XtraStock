#!/usr/bin/env python
"""Offline regression check for historical MMClaw finance tool usage."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "MMClaw"))

from mmclaw.tool_control import ToolLoopController  # noqa: E402
from mmclaw.tool_policy import is_tool_enabled  # noqa: E402


CONFIG = {
    "tool_profile": "finance_chat_readonly",
    "allowed_tools": [],
    "disabled_tools": [],
}


def iter_turn_calls(path: Path):
    calls = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                message = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if message.get("role") == "user" and calls:
                yield calls
                calls = []
            for call in message.get("tool_calls") or []:
                calls.append((call.get("name") or "", call.get("args") or {}))
    if calls:
        yield calls


def evaluate(paths: list[Path]) -> dict:
    totals = Counter()
    tool_counts = Counter()
    blocked_counts = Counter()
    turn_sizes = []
    for path in paths:
        totals["files"] += 1
        for calls in iter_turn_calls(path):
            totals["turns"] += 1
            turn_sizes.append(len(calls))
            control = ToolLoopController(max_rounds=6, max_calls=12, duplicate_limit=1)
            control.begin_round()
            for name, args in calls:
                totals["historical_calls"] += 1
                tool_counts[name] += 1
                if not is_tool_enabled(name, CONFIG):
                    totals["blocked_by_profile"] += 1
                    blocked_counts[name] += 1
                    continue
                decision = control.before_call(name, args)
                if not decision.execute:
                    totals["blocked_by_budget_or_duplicate"] += 1
                    blocked_counts[decision.reason or "budget"] += 1
                    continue
                control.after_call(name, '{"meta":{"row_count":1},"rows":[]}')
                totals["projected_executed_calls"] += 1
            if len(calls) > 12:
                totals["turns_over_call_budget"] += 1

    ordered_sizes = sorted(turn_sizes)
    p95_index = max(0, min(len(ordered_sizes) - 1, int(len(ordered_sizes) * 0.95))) if ordered_sizes else 0
    return {
        "summary": dict(totals),
        "turn_call_distribution": {
            "max": max(ordered_sizes, default=0),
            "median": ordered_sizes[len(ordered_sizes) // 2] if ordered_sizes else 0,
            "p95": ordered_sizes[p95_index] if ordered_sizes else 0,
        },
        "top_historical_tools": tool_counts.most_common(20),
        "projected_blocks": blocked_counts.most_common(20),
        "policy": {
            "profile": CONFIG["tool_profile"],
            "max_calls": 12,
            "max_rounds": 6,
            "exact_duplicate_limit": 1,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=ROOT / "MMClaw/workspaces",
        help="Directory containing historical messages.jsonl files.",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    paths = sorted(args.sessions_root.rglob("messages.jsonl"))
    report = evaluate(paths)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
