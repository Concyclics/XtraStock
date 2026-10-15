#!/usr/bin/env python3
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import datetime as dt
import json
import os
import urllib.request
from pathlib import Path
from typing import Any

from common import ROOT, connect, ensure_ops_schema, today, write_json


def fetch_rows() -> dict[str, Any]:
    with connect(admin=False) as conn:
        failed_runs = conn.execute(
            """
            SELECT task_name, source_name, started_at, finished_at, error, log_path
            FROM ops.crawler_runs failed
            WHERE failed.status = 'failed'
              AND failed.started_at >= now() - interval '48 hours'
              AND NOT EXISTS (
                SELECT 1
                FROM ops.crawler_runs resolved
                WHERE resolved.task_name = failed.task_name
                  AND resolved.source_name = failed.source_name
                  AND resolved.status = 'ok'
                  AND resolved.started_at > failed.started_at
              )
            ORDER BY started_at DESC
            LIMIT 50
            """
        ).fetchall()
        stale_sources = conn.execute(
            """
            SELECT source_name, max(newspaper_date) AS max_date, count(*) AS rows
            FROM news.articles
            WHERE source_name IN ('peoples_daily', 'xinhua_daily')
            GROUP BY source_name
            ORDER BY source_name
            """
        ).fetchall()
        report_max = conn.execute(
            "SELECT max(report_date) AS max_date, count(*) AS rows FROM research.reports"
        ).fetchone()
        ocr_status = conn.execute(
            "SELECT status, count(*) AS rows FROM ops.ocr_queue GROUP BY status ORDER BY status"
        ).fetchall()
        retry_plans = conn.execute(
            """
            SELECT id, plan_date, status, created_at, executed_at
            FROM ops.retry_plans
            WHERE created_at >= now() - interval '7 days'
              AND status IN ('pending', 'failed')
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()
        market_state = conn.execute(
            """
            SELECT status, count(*) AS rows
            FROM market.crawl_state
            WHERE job_name IN ('daily_midday', 'daily_close')
              AND last_cursor = current_date::text
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        market_coverage = conn.execute(
            """
            WITH active AS (
              SELECT count(*) AS rows
              FROM market.assets
              WHERE asset_type='stock'
                AND metadata ? 'eastmoney_secid'
                AND COALESCE(metadata #>> '{raw,price}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                AND (metadata #>> '{raw,price}')::numeric > 0
                AND COALESCE(metadata #>> '{raw,total_mv}', '') ~ '^-?[0-9]+(\\.[0-9]+)?$'
                AND (metadata #>> '{raw,total_mv}')::numeric > 0
            ), latest AS (
              SELECT max(last_cursor) AS cursor
              FROM market.crawl_state
              WHERE job_name='daily_close' AND source='eastmoney'
            ), covered AS (
              SELECT count(*) AS rows
              FROM market.crawl_state s
              CROSS JOIN latest l
              WHERE s.job_name='daily_close'
                AND s.source='eastmoney'
                AND s.last_cursor=l.cursor
                AND s.status='ok'
            )
            SELECT latest.cursor::date AS latest_bar_date,
                   active.rows AS active_assets,
                   covered.rows AS covered_assets,
                   greatest(active.rows-covered.rows, 0) AS missing_assets
            FROM active CROSS JOIN latest CROSS JOIN covered
            """
        ).fetchone()
        semantic_index = conn.execute(
            """
            SELECT count(*) AS queue_documents,
                   count(*) FILTER (WHERE status='ok') AS embedded_documents,
                   count(*) FILTER (WHERE status='pending') AS pending_documents,
                   count(*) FILTER (WHERE status='failed') AS failed_documents,
                   max(processed_at) FILTER (WHERE status='ok') AS latest_embedded_at
            FROM ops.document_embedding_queue
            """
        ).fetchone()
        kg_status = conn.execute(
            """
            SELECT max(as_of_date) AS latest_stats_date,
                   sum(relation_count) FILTER (WHERE as_of_date=(SELECT max(as_of_date) FROM analytics.kg_relation_daily_stats)) AS active_relations
            FROM analytics.kg_relation_daily_stats
            """
        ).fetchone()
    quality_path = ROOT / "logs/agent_quality/latest_quality_gate.json"
    try:
        agent_quality = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else None
    except (OSError, json.JSONDecodeError):
        agent_quality = None
    return {
        "failed_runs": failed_runs,
        "stale_sources": stale_sources,
        "report_max": report_max,
        "ocr_status": ocr_status,
        "retry_plans": retry_plans,
        "market_state": market_state,
        "market_coverage": market_coverage,
        "semantic_index": semantic_index,
        "kg_status": kg_status,
        "agent_quality": agent_quality,
    }


def is_stale(max_date: dt.date | None, allowed_lag_days: int) -> bool:
    if not max_date:
        return True
    return max_date < today() - dt.timedelta(days=allowed_lag_days)


def build_report(data: dict[str, Any], allowed_lag_days: int) -> tuple[dict[str, Any], str]:
    alerts: list[str] = []
    for row in data["stale_sources"]:
        if is_stale(row["max_date"], allowed_lag_days):
            alerts.append(f"news source stale: {row['source_name']} max_date={row['max_date']}")
    if is_stale(data["report_max"]["max_date"], 2):
        alerts.append(f"research reports stale: max_date={data['report_max']['max_date']}")
    if data["failed_runs"]:
        alerts.append(f"failed crawler runs in 48h: {len(data['failed_runs'])}")
    failed_ocr = sum(row["rows"] for row in data["ocr_status"] if row["status"] == "failed")
    if failed_ocr:
        alerts.append(f"OCR failed queue rows: {failed_ocr}")
    pending_retry = [row for row in data["retry_plans"] if row["status"] == "pending"]
    if pending_retry:
        alerts.append(f"pending retry plans: {len(pending_retry)}")
    failed_market = sum(row["rows"] for row in data["market_state"] if row["status"] == "failed")
    if failed_market:
        alerts.append(f"recent failed market crawl states: {failed_market}")
    market_coverage = data.get("market_coverage") or {}
    active_assets = int(market_coverage.get("active_assets") or 0)
    missing_assets = int(market_coverage.get("missing_assets") or 0)
    if market_coverage.get("latest_bar_date") and active_assets and missing_assets > max(50, active_assets // 100):
        alerts.append(
            f"daily K-line coverage incomplete: date={market_coverage.get('latest_bar_date')} "
            f"covered={market_coverage.get('covered_assets')}/{active_assets} missing={missing_assets}"
        )
    if data["semantic_index"] and data["semantic_index"]["failed_documents"]:
        alerts.append(f"failed document embeddings: {data['semantic_index']['failed_documents']}")
    if not data["agent_quality"]:
        alerts.append("finance agent quality gate has no latest report")
    elif not data["agent_quality"].get("passed"):
        alerts.append(
            f"finance agent quality gate failed: {data['agent_quality'].get('checks_passed')}/{data['agent_quality'].get('checks_total')}"
        )

    status = "alert" if alerts else "ok"
    summary = {
        "date": today().isoformat(),
        "status": status,
        "alerts": alerts,
        "news": data["stale_sources"],
        "reports": data["report_max"],
        "ocr": data["ocr_status"],
        "retry_plans": data["retry_plans"],
        "market": data["market_state"],
        "market_coverage": market_coverage,
        "failed_runs": data["failed_runs"],
        "semantic_index": data["semantic_index"],
        "knowledge_graph": data["kg_status"],
        "agent_quality": data["agent_quality"],
    }
    lines = [f"# Finance Pipeline Status {today().isoformat()}", "", f"status: **{status}**", ""]
    if alerts:
        lines.extend(["## Alerts", ""])
        lines.extend(f"- {item}" for item in alerts)
        lines.append("")
    lines.extend(["## News", ""])
    for row in data["stale_sources"]:
        lines.append(f"- {row['source_name']}: rows={row['rows']} max_date={row['max_date']}")
    lines.extend(["", "## Reports", ""])
    lines.append(f"- research.reports: rows={data['report_max']['rows']} max_date={data['report_max']['max_date']}")
    lines.extend(["", "## OCR Queue", ""])
    if data["ocr_status"]:
        lines.extend(f"- {row['status']}: {row['rows']}" for row in data["ocr_status"])
    else:
        lines.append("- no queue rows")
    lines.extend(["", "## Failed Runs", ""])
    if data["failed_runs"]:
        for row in data["failed_runs"]:
            lines.append(f"- {row['started_at']} {row['task_name']}/{row['source_name']}: {row['error']} ({row['log_path']})")
    else:
        lines.append("- none")
    lines.extend(["", "## Market Coverage", ""])
    if market_coverage.get("latest_bar_date"):
        lines.append(
            f"- latest daily-close state: date={market_coverage.get('latest_bar_date')} "
            f"covered={market_coverage.get('covered_assets')}/{market_coverage.get('active_assets')} "
            f"missing={market_coverage.get('missing_assets')}"
        )
    else:
        lines.append("- daily_close baseline not available yet; next close or Sina retry will initialize it")
    lines.extend(["", "## Agent Knowledge", ""])
    semantic = data["semantic_index"] or {}
    lines.append(
        f"- semantic embeddings: ok={semantic.get('embedded_documents')} pending={semantic.get('pending_documents')} failed={semantic.get('failed_documents')} latest={semantic.get('latest_embedded_at')}"
    )
    lines.append(
        f"- temporal KG: latest={data['kg_status'].get('latest_stats_date') if data['kg_status'] else None} active_relations={data['kg_status'].get('active_relations') if data['kg_status'] else None}"
    )
    quality = data["agent_quality"] or {}
    lines.append(
        f"- agent quality gate: passed={quality.get('passed')} checks={quality.get('checks_passed')}/{quality.get('checks_total')} generated_at={quality.get('generated_at')}"
    )
    return summary, "\n".join(lines) + "\n"


def post_webhook(url: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate finance pipeline status report and optional alert.")
    parser.add_argument("--allowed-news-lag-days", type=int, default=1)
    parser.add_argument("--webhook", default=os.getenv("FINANCE_ALERT_WEBHOOK", ""))
    parser.add_argument("--always-send", action="store_true")
    args = parser.parse_args()

    ensure_ops_schema()
    summary, markdown = build_report(fetch_rows(), args.allowed_news_lag_days)
    output_dir = ROOT / "logs/pipeline/status"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / f"status_{today().strftime('%Y%m%d')}.json", summary)
    md_path = output_dir / f"status_{today().strftime('%Y%m%d')}.md"
    md_path.write_text(markdown, encoding="utf-8")
    print(f"[status_report] wrote {md_path}")
    if args.webhook and (args.always_send or summary["status"] == "alert"):
        post_webhook(args.webhook, {"text": markdown, "summary": summary})
        print("[status_report] webhook sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
