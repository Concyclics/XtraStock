#!/usr/bin/env python3
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pipeline"))

from common import connect, load_env  # noqa: E402


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def meili_headers(master_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {master_key}"}


def pg_summary() -> dict[str, Any]:
    with connect(admin=False) as conn:
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        row = conn.execute(
            """
            SELECT
              (SELECT count(*) FROM corpus.documents) AS documents,
              (SELECT count(*) FROM corpus.document_search_index) AS postgres_search_documents,
              (SELECT count(*) FROM corpus.documents d
                WHERE NOT EXISTS (
                  SELECT 1 FROM corpus.document_search_index s WHERE s.document_id = d.id
                )) AS missing_postgres_search,
              (SELECT max(indexed_at) FROM corpus.document_search_index) AS newest_indexed_at,
              (SELECT coalesce(jsonb_object_agg(status, rows), '{}'::jsonb)
                FROM (
                  SELECT status, count(*) AS rows
                  FROM ops.document_index_queue
                  GROUP BY status
                ) queue_counts) AS queue_counts
            """
        ).fetchone()
    return dict(row)


def meili_stats(base_url: str, master_key: str, index_uid: str) -> dict[str, Any]:
    response = requests.get(
        f"{base_url.rstrip('/')}/indexes/{index_uid}/stats",
        headers=meili_headers(master_key),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def meili_document_ids(
    base_url: str,
    master_key: str,
    index_uid: str,
    published_since_ts: int | None = None,
) -> set[str]:
    base = base_url.rstrip("/")
    offset = 0
    limit = 1000
    values: set[str] = set()
    while True:
        params: dict[str, Any] = {"offset": offset, "limit": limit, "fields": "id"}
        if published_since_ts is not None:
            params["filter"] = f"published_at >= {published_since_ts}"
        response = requests.get(
            f"{base}/indexes/{index_uid}/documents",
            headers=meili_headers(master_key),
            params=params,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") or []
        values.update(str(item["id"]) for item in results if item.get("id") is not None)
        offset += len(results)
        if not results or offset >= int(payload.get("total", 0)):
            break
    return values


def postgres_document_ids(days: int | None = None) -> set[str]:
    where = ""
    params: tuple[int, ...] = ()
    if days is not None:
        where = """
          WHERE d.published_at >= (
            ((now() AT TIME ZONE 'Asia/Shanghai')::date - (%s - 1))::timestamp
            AT TIME ZONE 'Asia/Shanghai'
          )
        """
        params = (days,)
    with connect(admin=False) as conn:
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        rows = conn.execute(
            f"""
            SELECT s.document_id::text AS document_id
            FROM corpus.document_search_index s
            JOIN corpus.documents d ON d.id = s.document_id
            {where}
            """,
            params,
        ).fetchall()
    return {str(row["document_id"]) for row in rows}


def recent_meili_cutoff(days: int) -> int:
    local_day = dt.datetime.now(LOCAL_TZ).date() - dt.timedelta(days=days - 1)
    return int(dt.datetime.combine(local_day, dt.time.min, tzinfo=LOCAL_TZ).timestamp())


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify PostgreSQL FTS coverage and the recent Meilisearch cache.")
    parser.add_argument("--full", action="store_true", help="Compare Meilisearch ids against the expected recent cache set.")
    parser.add_argument(
        "--meili-days",
        type=int,
        default=2,
        help="Published-date window expected in Meilisearch (default: 2). Use 0 to skip id comparison.",
    )
    parser.add_argument("--meili-url", default=os.getenv("MEILI_URL", "http://127.0.0.1:17700"))
    parser.add_argument("--index", default=os.getenv("MEILI_INDEX", "finance_recent_documents"))
    args = parser.parse_args()
    if args.meili_days < 0:
        raise SystemExit("--meili-days must be >= 0")

    env = load_env()
    master_key = os.getenv("MEILI_MASTER_KEY", env.get("MEILI_MASTER_KEY", ""))
    if not master_key:
        raise SystemExit("missing MEILI_MASTER_KEY")

    summary = pg_summary()
    stats = meili_stats(args.meili_url, master_key, args.index)
    summary["meilisearch_documents"] = stats.get("numberOfDocuments")
    summary["meilisearch_is_indexing"] = stats.get("isIndexing")
    summary["count_delta_postgres_minus_meili"] = (
        int(summary["postgres_search_documents"]) - int(stats.get("numberOfDocuments") or 0)
    )
    if args.full and args.meili_days:
        postgres_ids = postgres_document_ids(args.meili_days)
        meili_ids = meili_document_ids(
            args.meili_url,
            master_key,
            args.index,
            recent_meili_cutoff(args.meili_days),
        )
        summary["meili_expected_days"] = args.meili_days
        summary["expected_recent_meili_documents"] = len(postgres_ids)
        summary["actual_recent_meili_documents"] = len(meili_ids)
        summary["recent_postgres_missing_in_meili"] = len(postgres_ids - meili_ids)
        summary["recent_meili_missing_in_postgres"] = len(meili_ids - postgres_ids)
        summary["sample_recent_postgres_missing_in_meili"] = sorted(postgres_ids - meili_ids)[:20]
        summary["sample_recent_meili_missing_in_postgres"] = sorted(meili_ids - postgres_ids)[:20]
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
