#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from psycopg.types.json import Jsonb

from common import ROOT, connect, today
from user_presentation import render_report_file


QQ_CONFIG = ROOT / "MMClaw/workspaces/qq_agent/mmclaw.json"


def split_text(text: str, limit: int = 3400) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in text.splitlines(keepends=True):
        pieces = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            if current and length + len(piece) > limit:
                chunks.append("".join(current))
                current, length = [], 0
            current.append(piece)
            length += len(piece)
    if current:
        chunks.append("".join(current))
    return [chunk for chunk in chunks if chunk]


def qq_groups() -> list[str]:
    config = json.loads(QQ_CONFIG.read_text(encoding="utf-8"))
    qq = config.get("connectors", {}).get("qqbot", {})
    values = [
        qq.get("daily_report_group_openids"), qq.get("daily_report_group_openid"),
        qq.get("daily_push_group_openid"), qq.get("default_group_openid"),
    ]
    groups: list[str] = []
    for value in values:
        for item in value if isinstance(value, list) else ([value] if value else []):
            text = str(item or "").strip()
            if text and text not in groups:
                groups.append(text)
    return groups


def brief_from_report(text: str) -> str:
    match = __import__("re").search(r"(?ms)^## 省流版\s*(.*?)(?=^## |\Z)", text)
    body = match.group(1).strip() if match else "日报已生成，请阅读完整分段。"
    return "AI硬件日报省流版\n\n" + body


def main() -> int:
    parser = argparse.ArgumentParser(description="Enqueue a validated V3 daily report for QQ exactly once.")
    parser.add_argument("--date")
    parser.add_argument("--allow-shadow", action="store_true")
    args = parser.parse_args()
    report_date = dt.date.fromisoformat(args.date) if args.date else today()
    with connect(admin=False) as conn:
        row = conn.execute(
            """
            SELECT report_run_id,status,output_path,validation
            FROM agent.report_runs
            WHERE report_type='ai_hardware_daily_v3_shadow' AND report_date=%s
              AND status IN ('published','degraded')
            ORDER BY completed_at DESC LIMIT 1
            """,
            (report_date,),
        ).fetchone()
    if not row:
        raise SystemExit(f"no publishable V3 report for {report_date}")
    if not args.allow_shadow and bool((row["validation"] or {}).get("shadow", True)):
        raise SystemExit("refusing to enqueue a shadow report; publish it first")
    path = Path(row["output_path"])
    text = render_report_file(path)
    full_chunks = split_text(text)
    messages = [("daily_brief", brief_from_report(text)), *[("daily_report", chunk) for chunk in full_chunks]]
    inserted = 0
    groups = qq_groups()
    with connect(admin=False) as conn:
        for group_id in groups:
            for index, (kind, content) in enumerate(messages):
                result = conn.execute(
                    """
                    INSERT INTO agent.message_outbox (
                      channel,target_type,target_id,message_kind,dedup_key,payload,priority,metadata
                    ) VALUES ('qq','group',%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (channel,target_type,target_id,dedup_key) DO NOTHING
                    """,
                    (
                        group_id, kind, f"ai-hardware-v3:{report_date}:{kind}:{index}",
                        Jsonb({
                            "text": content, "report_date": report_date.isoformat(),
                            "report_run_id": str(row["report_run_id"]), "part": index,
                            "parts": len(messages),
                        }),
                        20 if kind == "daily_brief" else 30,
                        Jsonb({"source_path": str(path)}),
                    ),
                )
                inserted += max(result.rowcount, 0)
    print(json.dumps({
        "report_date": report_date.isoformat(), "group_count": len(groups),
        "messages_per_group": len(messages), "inserted": inserted,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
