#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from psycopg.types.json import Jsonb

from common import ROOT, connect, ensure_ops_schema, recover_stale_crawler_runs, today, write_json


def date_list(start: dt.date, end: dt.date) -> list[str]:
    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y%m%d"))
        current += dt.timedelta(days=1)
    return dates


def max_news(source: str) -> dt.date | None:
    with connect(admin=False) as conn:
        row = conn.execute(
            "SELECT max(newspaper_date) AS max_date, count(*) AS rows FROM news.articles WHERE source_name = %s",
            (source,),
        ).fetchone()
    return row["max_date"] if row and row["max_date"] else None


def sqlite_count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def sqlite_max_report_date(db_path: Path, table: str) -> dt.date | None:
    conn = sqlite3.connect(db_path)
    try:
        value = conn.execute(f'SELECT max("日期") FROM {table}').fetchone()[0]
    finally:
        conn.close()
    if not value:
        return None
    return dt.date.fromisoformat(str(value)[:10])


def pg_count(sql: str, params=()) -> int:
    with connect(admin=False) as conn:
        row = conn.execute(sql, params).fetchone()
    return int(next(iter(row.values()))) if row else 0


def missing_page_pdfs(source: str, lookback_days: int = 7) -> list[str]:
    root = ROOT / "newspaper_pdfs" / source
    cutoff = today() - dt.timedelta(days=lookback_days)
    db_path = ROOT / ("Spider/PeoplesDaily/peoples_daily.sqlite3" if source == "peoples_daily" else "Spider/XinhuaDaily/xinhua_daily.sqlite3")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT date, page_no
        FROM pages
        WHERE date >= ?
          AND COALESCE(pdf_url, '') != ''
        """,
        (cutoff.strftime("%Y%m%d"),),
    ).fetchall()
    conn.close()
    missing_dates = set()
    for row in rows:
        date_text = str(row["date"])
        pattern = root / date_text[:4] / date_text / f"Page{str(row['page_no']).zfill(2)}.pdf"
        if not pattern.exists():
            missing_dates.add(date_text)
    return sorted(missing_dates)


def build_plan() -> dict:
    now = today()
    plan: dict = {
        "plan_date": now.isoformat(),
        "news": {},
        "reports": {},
        "ocr": {},
        "search": {},
        "market": {},
        "analytics": {},
    }

    for source in ("peoples_daily", "xinhua_daily"):
        latest = max_news(source)
        dates = []
        if latest and latest < now:
            dates = date_list(latest + dt.timedelta(days=1), now)
        elif latest is None:
            dates = [now.strftime("%Y%m%d")]
        plan["news"][source] = {
            "latest_date": latest.isoformat() if latest else None,
            "missing_dates": dates,
            "missing_page_pdf_dates": missing_page_pdfs(source),
        }

    local_stock = sqlite_count(ROOT / "eastmoney_reports.sqlite3", "stock_reports")
    local_industry = sqlite_count(ROOT / "eastmoney_reports.sqlite3", "industry_reports")
    local_stock_latest = sqlite_max_report_date(ROOT / "eastmoney_reports.sqlite3", "stock_reports")
    local_industry_latest = sqlite_max_report_date(ROOT / "eastmoney_reports.sqlite3", "industry_reports")
    local_latest_candidates = [d for d in (local_stock_latest, local_industry_latest) if d]
    local_latest = max(local_latest_candidates) if local_latest_candidates else None
    pg_stock = pg_count("SELECT count(*) FROM research.reports WHERE report_type = 'stock'")
    pg_industry = pg_count("SELECT count(*) FROM research.reports WHERE report_type = 'industry'")
    with connect(admin=False) as conn:
        report_row = conn.execute("SELECT max(report_date) AS max_date FROM research.reports").fetchone()
    report_latest = report_row["max_date"] if report_row and report_row["max_date"] else None
    plan["reports"] = {
        "pg_latest_date": report_latest.isoformat() if report_latest else None,
        "local_latest_date": local_latest.isoformat() if local_latest else None,
        "local_stock_rows": local_stock,
        "local_industry_rows": local_industry,
        "pg_stock_rows": pg_stock,
        "pg_industry_rows": pg_industry,
        "needs_import": bool(local_latest and (not report_latest or report_latest < local_latest)),
    }

    with connect(admin=False) as conn:
        ocr_rows = conn.execute(
            """
            SELECT status, count(*) AS rows
            FROM ops.ocr_queue
            GROUP BY status
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
        latest_close_run = conn.execute(
            """
            SELECT status, start_date, started_at, finished_at, error
            FROM ops.crawler_runs
            WHERE task_name='daily_market_close'
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
    plan["ocr"] = {row["status"]: row["rows"] for row in ocr_rows}
    with connect(admin=False) as conn:
        search_row = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM corpus.documents d
                WHERE NOT EXISTS (
                  SELECT 1 FROM corpus.document_search_index s WHERE s.document_id = d.id
                )) AS missing_documents,
              (SELECT max(indexed_at) FROM corpus.document_search_index) AS newest_indexed_at,
              (SELECT coalesce(jsonb_object_agg(status, rows), '{}'::jsonb)
                FROM (
                  SELECT status, count(*) AS rows
                  FROM ops.document_index_queue
                  GROUP BY status
                ) q) AS queue_counts
            """
        ).fetchone()
    plan["search"] = dict(search_row) if search_row else {}
    with connect(admin=False) as conn:
        semantic_row = conn.execute(
            """
            SELECT count(*) AS queue_documents,
                   count(*) FILTER (WHERE status='ok') AS embedded_documents,
                   count(*) FILTER (WHERE status='pending') AS pending_documents,
                   count(*) FILTER (WHERE status='failed') AS failed_documents,
                   max(processed_at) FILTER (WHERE status='ok') AS latest_embedded_at
            FROM ops.document_embedding_queue
            """
        ).fetchone()
        kg_row = conn.execute(
            "SELECT max(as_of_date) AS latest_stats_date FROM analytics.kg_relation_daily_stats"
        ).fetchone()
    plan["search"]["semantic_embeddings"] = dict(semantic_row) if semantic_row else {}
    plan["knowledge_graph"] = dict(kg_row) if kg_row else {}
    with connect(admin=False) as conn:
        analytics_row = conn.execute(
            """
            WITH latest_runs AS (
              SELECT DISTINCT ON (task_name)
                     task_name, status, end_date, started_at, finished_at, error
              FROM ops.crawler_runs
              WHERE task_name IN (
                'daily_analytics_core', 'daily_knowledge_graph',
                'daily_factor_materialize'
              )
              ORDER BY task_name, started_at DESC
            ), run_json AS (
              SELECT coalesce(
                jsonb_object_agg(task_name, to_jsonb(latest_runs) - 'task_name'),
                '{}'::jsonb
              ) AS runs
              FROM latest_runs
            )
            SELECT
              run_json.runs,
              (SELECT max(dt) FROM analytics.asset_technical_daily) AS technical_date,
              (SELECT max(dt) FROM analytics.graph_feature_daily) AS graph_feature_date,
              (SELECT max(dt) FROM factor.feature_daily) AS factor_feature_date,
              (SELECT max(dt) FROM factor.daily_scores) AS factor_score_date,
              (SELECT count(*) FROM analytics.graph_feature_daily g
                WHERE NOT EXISTS (
                  SELECT 1 FROM analytics.asset_technical_daily t
                  WHERE t.asset_id=g.asset_id AND t.dt=g.dt
                )) AS orphan_graph_feature_rows
            FROM run_json
            """
        ).fetchone()
    plan["analytics"] = dict(analytics_row) if analytics_row else {}
    market = dict(market_coverage) if market_coverage else {}
    active_assets = int(market.get("active_assets") or 0)
    missing_assets = int(market.get("missing_assets") or 0)
    tolerance = max(50, active_assets // 100) if active_assets else 0
    latest_close = dict(latest_close_run) if latest_close_run else {}
    market.update({
        "latest_close_run": latest_close,
        "missing_tolerance": tolerance,
        "needs_retry": bool(latest_close.get("status") == "failed" or missing_assets > tolerance),
        "target_date": str(market.get("latest_bar_date") or latest_close.get("start_date") or now),
        "stale_or_failed_state_rows": missing_assets,
    })
    plan["market"] = market
    return plan


def save_plan(plan: dict) -> None:
    output = ROOT / "build/retry_plans" / f"retry_plan_{today().strftime('%Y%m%d')}.json"
    write_json(output, plan)
    db_plan = json.loads(json.dumps(plan, ensure_ascii=False, default=str))
    with connect(admin=False) as conn:
        conn.execute(
            """
            UPDATE ops.retry_plans
            SET status='skipped', executed_at=now(),
                result=result || '{"reason":"superseded by newer plan for same date"}'::jsonb
            WHERE plan_date=%s AND status='pending'
            """,
            (today(),),
        )
        conn.execute(
            """
            INSERT INTO ops.retry_plans(plan_date, status, plan)
            VALUES (%s, 'pending', %s)
            """,
            (today(), Jsonb(db_plan)),
        )
    print(f"[nightly_verify] wrote {output}")


def recover_stale_pipeline_runs() -> int:
    return recover_stale_crawler_runs(stale_minutes=12 * 60)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify daily pipeline coverage and generate a retry plan.")
    parser.parse_args()
    ensure_ops_schema()
    recovered = recover_stale_pipeline_runs()
    if recovered:
        print(f"[nightly_verify] recovered stale crawler runs={recovered}")
    plan = build_plan()
    save_plan(plan)
    print("[nightly_verify] complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
