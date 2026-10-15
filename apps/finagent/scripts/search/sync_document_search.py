#!/usr/bin/env python3
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pipeline"))

from common import connect, ensure_ops_schema, load_env, today  # noqa: E402


DEFAULT_INDEX = "finance_recent_documents"
DEFAULT_BODY_CHARS = 6000
# The complete text remains in the trigram index. PostgreSQL limits a single
# tsvector to 1 MiB; bound its ranking prefix without truncating searchable text.
MAX_FTS_VECTOR_CHARS = 24000
DEFAULT_MEILI_BODY_CHARS = 1800
DEFAULT_MEILI_TASK_TIMEOUT_SECONDS = 300
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def batched(items: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def clean_text(value: Any, max_chars: int | None = None) -> str:
    text = str(value or "")
    text = " ".join(text.replace("\x00", " ").split())
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars]
    return text


def unique_text(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def meili_headers(master_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {master_key}", "Content-Type": "application/json"}


def meili_request(method: str, url: str, master_key: str, **kwargs: Any) -> requests.Response:
    response = requests.request(method, url, headers=meili_headers(master_key), timeout=60, **kwargs)
    response.raise_for_status()
    return response


def meili_task_uid(response: requests.Response) -> int | None:
    payload = response.json()
    value = payload.get("taskUid", payload.get("uid"))
    return int(value) if value is not None else None


def wait_meili_tasks(
    base_url: str,
    master_key: str,
    task_uids: Iterable[int | None],
    timeout_seconds: int = DEFAULT_MEILI_TASK_TIMEOUT_SECONDS,
) -> None:
    pending = {task_uid for task_uid in task_uids if task_uid is not None}
    if not pending:
        return

    base = base_url.rstrip("/")
    deadline = time.monotonic() + timeout_seconds
    while pending:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Meilisearch tasks did not finish before timeout: {sorted(pending)}")
        finished: list[int] = []
        for task_uid in pending:
            response = meili_request("GET", f"{base}/tasks/{task_uid}", master_key)
            payload = response.json()
            status = payload.get("status")
            if status == "succeeded":
                finished.append(task_uid)
            elif status in {"failed", "canceled"}:
                raise RuntimeError(
                    f"Meilisearch task {task_uid} {status}: {payload.get('error') or payload}"
                )
        pending.difference_update(finished)
        if pending:
            time.sleep(0.5)


def meili_settings_match(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    unordered = {"displayedAttributes", "filterableAttributes", "sortableAttributes"}
    for key, expected_value in expected.items():
        current_value = current.get(key)
        if key in unordered:
            if set(current_value or []) != set(expected_value or []):
                return False
        elif current_value != expected_value:
            return False
    return True


def ensure_meili_index(base_url: str, master_key: str, index_uid: str) -> None:
    base = base_url.rstrip("/")
    response = requests.get(f"{base}/indexes/{index_uid}", headers=meili_headers(master_key), timeout=20)
    if response.status_code == 404:
        created = meili_request("POST", f"{base}/indexes", master_key, json={"uid": index_uid, "primaryKey": "id"})
        wait_meili_tasks(base, master_key, [meili_task_uid(created)])
    elif response.status_code >= 400:
        response.raise_for_status()

    settings = {
        "searchableAttributes": [
            "title",
            "keywords",
            "event_summaries",
            "body",
            "source_name",
            "institution",
            "stock_name",
            "industry_name",
        ],
        "displayedAttributes": [
            "id",
            "source_type",
            "source_name",
            "published_at",
            "published_date",
            "title",
            "url",
            "keywords",
            "stock_codes",
            "stock_name",
            "industries",
            "industry_name",
            "rating",
            "rating_change",
            "institution",
            "event_types",
            "event_summaries",
            "body",
        ],
        "filterableAttributes": [
            "source_type",
            "source_name",
            "published_at",
            "published_date",
            "keywords",
            "stock_codes",
            "industries",
            "event_types",
            "rating",
            "rating_change",
            "institution",
            "industry_name",
        ],
        "sortableAttributes": ["published_at", "published_date"],
        "rankingRules": [
            "words",
            "typo",
            "proximity",
            "attribute",
            "sort",
            "exactness",
            "published_at:desc",
        ],
    }
    current = meili_request("GET", f"{base}/indexes/{index_uid}/settings", master_key).json()
    if not meili_settings_match(current, settings):
        updated = meili_request("PATCH", f"{base}/indexes/{index_uid}/settings", master_key, json=settings)
        wait_meili_tasks(base, master_key, [meili_task_uid(updated)])


def fetch_documents(
    since: dt.date | None,
    until: dt.date | None,
    limit: int,
    offset: int,
    body_chars: int,
    missing_only: bool,
    document_ids: list[Any] | None = None,
    cursor_published_at: dt.datetime | None = None,
    cursor_document_id: Any | None = None,
) -> list[dict[str, Any]]:
    where = []
    params: dict[str, Any] = {"limit": limit, "offset": offset, "body_chars": body_chars}
    if since:
        where.append("d.published_at >= (%(since)s::date::timestamp AT TIME ZONE 'Asia/Shanghai')")
        params["since"] = since
    if until:
        where.append("d.published_at < ((%(until)s::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')")
        params["until"] = until
    if missing_only:
        where.append("NOT EXISTS (SELECT 1 FROM corpus.document_search_index s WHERE s.document_id = d.id)")
    if document_ids:
        where.append("d.id = ANY(%(document_ids)s::uuid[])")
        params["document_ids"] = document_ids
    if cursor_document_id is not None:
        if cursor_published_at is None:
            where.append("d.published_at IS NULL AND d.id > %(cursor_document_id)s")
        else:
            where.append(
                "(d.published_at < %(cursor_published_at)s OR d.published_at IS NULL "
                "OR (d.published_at = %(cursor_published_at)s AND d.id > %(cursor_document_id)s))"
            )
            params["cursor_published_at"] = cursor_published_at
        params["cursor_document_id"] = cursor_document_id
    where.append("NOT EXISTS (SELECT 1 FROM corpus.document_quarantine q WHERE q.document_id=d.id AND q.released_at IS NULL)")
    where.append("NOT EXISTS (SELECT 1 FROM corpus.document_quality dq WHERE dq.document_id=d.id AND dq.quality_status IN ('template_noise','parse_failed','quarantined'))")
    where_sql = " AND ".join(where) if where else "true"

    sql = f"""
      WITH base AS (
        SELECT d.id, d.source_type, d.source_name, d.external_id, d.published_at,
               d.title, d.url, d.updated_at,
               (d.metadata #>> '{{attachment_extraction,search_full_body}}' = 'true'
                 OR (d.source_type='announcement' AND d.source_name='cninfo' AND d.metadata->>'official_source'='true')) AS full_body_search,
               CASE WHEN d.metadata #>> '{{attachment_extraction,search_full_body}}' = 'true'
                         OR (d.source_type='announcement' AND d.source_name='cninfo' AND d.metadata->>'official_source'='true')
                    THEN coalesce(nullif(d.content_text, ''), nullif(d.content_md,''), d.raw_text, '')
                    ELSE left(coalesce(nullif(d.content_text, ''), nullif(d.content_md,''), d.raw_text, ''), %(body_chars)s)
               END AS body,
               r.report_type, r.report_date, r.institution, r.rating, r.rating_change,
               r.industry_name,r.identity_known_at,r.source_stock_code,
               coalesce(nullif(r.stock_code,''),
                 CASE WHEN d.source_name='cninfo'
                        AND d.metadata->>'official_source'='true'
                        AND d.metadata->>'stock_code' ~ '^[0-9]{{6}}$'
                      THEN d.metadata->>'stock_code' END) AS stock_code,
               coalesce(nullif(r.stock_name,''),
                 CASE WHEN d.source_name='cninfo'
                        AND d.metadata->>'official_source'='true'
                        AND d.metadata->>'stock_code' ~ '^[0-9]{{6}}$'
                      THEN d.metadata->>'stock_name' END) AS stock_name,
               r.report_title
        FROM corpus.documents d
        LEFT JOIN research.reports_resolved r ON r.document_id = d.id
        WHERE {where_sql}
        ORDER BY d.published_at DESC NULLS LAST, d.id
        LIMIT %(limit)s OFFSET %(offset)s
      ),
      kw AS (
        SELECT k.document_id,
               array_agg(k.keyword ORDER BY coalesce(k.weight, 0) DESC, k.keyword) AS keywords
        FROM corpus.document_keywords k
        JOIN base b ON b.id = k.document_id
        GROUP BY k.document_id
      ),
      ev AS (
        SELECT e.document_id,
               array_agg(DISTINCT e.event_type) AS event_types,
               array_agg(DISTINCT sc.stock_code) FILTER (WHERE sc.stock_code IS NOT NULL AND sc.stock_code <> '') AS event_stock_codes,
               array_agg(DISTINCT ind.industry) FILTER (WHERE ind.industry IS NOT NULL AND ind.industry <> '') AS event_industries,
               array_agg(DISTINCT keyword.keyword) FILTER (WHERE keyword.keyword IS NOT NULL AND keyword.keyword <> '') AS event_keywords,
               string_agg(DISTINCT left(coalesce(e.summary, e.evidence, ''), 300), E'\\n') AS event_summaries,
               max(e.updated_at) AS max_event_updated_at
        FROM analytics.document_events e
        JOIN base b ON b.id = e.document_id
        LEFT JOIN LATERAL unnest(e.stock_codes) AS sc(stock_code) ON true
        LEFT JOIN LATERAL unnest(e.industries) AS ind(industry) ON true
        LEFT JOIN LATERAL unnest(e.keywords) AS keyword(keyword) ON true
        GROUP BY e.document_id
      )
      SELECT b.*,
             coalesce(kw.keywords, ARRAY[]::text[]) AS keywords,
             coalesce(ev.event_types, ARRAY[]::text[]) AS event_types,
             coalesce(ev.event_stock_codes, ARRAY[]::text[]) AS event_stock_codes,
             coalesce(ev.event_industries, ARRAY[]::text[]) AS event_industries,
             coalesce(ev.event_keywords, ARRAY[]::text[]) AS event_keywords,
             coalesce(ev.event_summaries, '') AS event_summaries,
             greatest(b.updated_at, b.identity_known_at, ev.max_event_updated_at) AS source_updated_at
      FROM base b
      LEFT JOIN kw ON kw.document_id = b.id
      LEFT JOIN ev ON ev.document_id = b.id
      ORDER BY b.published_at DESC NULLS LAST, b.id
    """
    with connect(admin=False) as conn:
        # These bounded lookups do not benefit from parallel scans, while Docker's
        # default /dev/shm is too small for their dynamic shared-memory workers.
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        conn.row_factory = dict_row
        return list(conn.execute(sql, params).fetchall())


def build_search_row(row: dict[str, Any], body_chars: int) -> dict[str, Any]:
    title = clean_text(row.get("title") or row.get("report_title"))
    body = clean_text(row.get("body"), None if row.get("full_body_search") else body_chars)
    keywords = unique_text([*(row.get("keywords") or []), *(row.get("event_keywords") or [])])
    stock_codes = unique_text([row.get("stock_code"), *(row.get("event_stock_codes") or [])])
    industries = unique_text([row.get("industry_name"), *(row.get("event_industries") or [])])
    event_types = unique_text(row.get("event_types") or [])
    metadata = {
        "report_type": row.get("report_type"),
        "report_date": row.get("report_date").isoformat() if row.get("report_date") else None,
        "institution": row.get("institution"),
        "rating": row.get("rating"),
        "rating_change": row.get("rating_change"),
        "stock_name": row.get("stock_name"),
        "industry_name": row.get("industry_name"),
        "full_body_search": bool(row.get("full_body_search")),
        "identity_known_at": row.get("identity_known_at"),
        "source_stock_code": row.get("source_stock_code"),
    }
    event_summaries = clean_text(row.get("event_summaries"), 3000)
    search_parts = [
        title,
        " ".join(keywords),
        " ".join(stock_codes),
        " ".join(industries),
        row.get("stock_name") or "",
        row.get("institution") or "",
        row.get("rating") or "",
        row.get("rating_change") or "",
        event_summaries,
        body,
    ]
    search_text = clean_text(" ".join(search_parts))
    return {
        "document_id": row["id"],
        "source_type": row["source_type"],
        "source_name": row["source_name"],
        "external_id": row.get("external_id"),
        "published_at": row.get("published_at"),
        "title": title,
        "url": row.get("url"),
        "keywords": keywords,
        "stock_codes": stock_codes,
        "industries": industries,
        "event_types": event_types,
        "search_text": search_text,
        "body_chars": len(body),
        "source_updated_at": row.get("source_updated_at"),
        "metadata": metadata,
        "body": body,
        "event_summaries": event_summaries,
    }


def numeric_search_text(text: str) -> str:
    # PostgreSQL's simple parser can keep Chinese+number as a single lexeme
    # (e.g. 同比增长119.38). Add numeric terms without altering displayed text.
    tokens = re.findall(r"(?<![0-9.])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?![0-9.])", text)
    return " ".join(dict.fromkeys(token.replace(",", "").lstrip("+") for token in tokens))


def upsert_fts(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sql = """
      INSERT INTO corpus.document_search_index (
        document_id, source_type, source_name, external_id, published_at, title, url,
        keywords, stock_codes, industries, event_types, search_text, search_vector,
        body_chars, source_updated_at, metadata, indexed_at
      )
      VALUES (
        %(document_id)s, %(source_type)s, %(source_name)s, %(external_id)s, %(published_at)s,
        %(title)s, %(url)s, %(keywords)s, %(stock_codes)s, %(industries)s, %(event_types)s,
        %(search_text)s,
        setweight(to_tsvector('simple', coalesce(%(title)s, '')), 'A')
          || setweight(to_tsvector('simple', array_to_string(%(keywords)s::text[], ' ')), 'A')
          || setweight(to_tsvector('simple', array_to_string(%(stock_codes)s::text[], ' ')), 'A')
          || setweight(to_tsvector('simple', array_to_string(%(industries)s::text[], ' ')), 'A')
          || setweight(to_tsvector('simple', coalesce(%(fts_text)s, '')), 'B')
          || setweight(to_tsvector('simple', %(numeric_text)s), 'B'),
        %(body_chars)s, %(source_updated_at)s, %(metadata)s::jsonb, now()
      )
      ON CONFLICT (document_id) DO UPDATE SET
        source_type = EXCLUDED.source_type,
        source_name = EXCLUDED.source_name,
        external_id = EXCLUDED.external_id,
        published_at = EXCLUDED.published_at,
        title = EXCLUDED.title,
        url = EXCLUDED.url,
        keywords = EXCLUDED.keywords,
        stock_codes = EXCLUDED.stock_codes,
        industries = EXCLUDED.industries,
        event_types = EXCLUDED.event_types,
        search_text = EXCLUDED.search_text,
        search_vector = EXCLUDED.search_vector,
        body_chars = EXCLUDED.body_chars,
        source_updated_at = EXCLUDED.source_updated_at,
        metadata = EXCLUDED.metadata,
        indexed_at = now()
      WHERE (corpus.document_search_index.source_updated_at IS NULL
             OR EXCLUDED.source_updated_at >= corpus.document_search_index.source_updated_at)
        AND ROW(
        corpus.document_search_index.source_type,
        corpus.document_search_index.source_name,
        corpus.document_search_index.external_id,
        corpus.document_search_index.published_at,
        corpus.document_search_index.title,
        corpus.document_search_index.url,
        corpus.document_search_index.keywords,
        corpus.document_search_index.stock_codes,
        corpus.document_search_index.industries,
        corpus.document_search_index.event_types,
        corpus.document_search_index.search_text,
        corpus.document_search_index.search_vector,
        corpus.document_search_index.body_chars,
        corpus.document_search_index.source_updated_at,
        corpus.document_search_index.metadata
      ) IS DISTINCT FROM ROW(
        EXCLUDED.source_type,
        EXCLUDED.source_name,
        EXCLUDED.external_id,
        EXCLUDED.published_at,
        EXCLUDED.title,
        EXCLUDED.url,
        EXCLUDED.keywords,
        EXCLUDED.stock_codes,
        EXCLUDED.industries,
        EXCLUDED.event_types,
        EXCLUDED.search_text,
        EXCLUDED.search_vector,
        EXCLUDED.body_chars,
        EXCLUDED.source_updated_at,
        EXCLUDED.metadata
      )
    """
    payload = []
    for row in rows:
        prefix = row['search_text'][:MAX_FTS_VECTOR_CHARS]
        metadata = {
            **row['metadata'],
            'fts_vector_prefix_only': len(row['search_text']) > MAX_FTS_VECTOR_CHARS,
            'fts_vector_prefix_chars': len(prefix),
            'full_text_search_backend': 'pg_trgm_and_bounded_tsvector',
        }
        payload.append(
            {
                **row,
                "fts_text": prefix,
                "numeric_text": numeric_search_text(prefix),
                "metadata": json.dumps(metadata, ensure_ascii=False, default=str),
            }
        )
    with connect(admin=False) as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, payload)


def meili_doc(row: dict[str, Any], body_chars: int) -> dict[str, Any]:
    published_at = row.get("published_at")
    if published_at:
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=LOCAL_TZ)
        published_local = published_at.astimezone(LOCAL_TZ)
        published_ts = int(published_at.timestamp())
        published_date = published_local.date().isoformat()
        published_iso = published_local.isoformat()
    else:
        published_ts = 0
        published_date = None
        published_iso = None
    metadata = row.get("metadata") or {}
    return {
        "id": str(row["document_id"]),
        "source_type": row["source_type"],
        "source_name": row["source_name"],
        "external_id": row.get("external_id"),
        "published_at": published_ts,
        "published_iso": published_iso,
        "published_date": published_date,
        "title": row.get("title"),
        "url": row.get("url"),
        "keywords": row.get("keywords") or [],
        "stock_codes": row.get("stock_codes") or [],
        "stock_name": metadata.get("stock_name"),
        "industries": row.get("industries") or [],
        "industry_name": metadata.get("industry_name"),
        "rating": metadata.get("rating"),
        "rating_change": metadata.get("rating_change"),
        "institution": metadata.get("institution"),
        "event_types": row.get("event_types") or [],
        "event_summaries": row.get("event_summaries") or "",
        # PostgreSQL FTS retains the longer body. Keep Meilisearch's fast-recall
        # representation compact enough for the local low-power host.
        "body": clean_text(row.get("body"), body_chars),
    }


def upsert_meili(
    rows: list[dict[str, Any]],
    base_url: str,
    master_key: str,
    index_uid: str,
    batch_size: int,
    body_chars: int,
) -> int:
    if not rows:
        return 0
    base = base_url.rstrip("/")
    count = 0
    task_uids: list[int | None] = []
    for chunk in batched([meili_doc(row, body_chars) for row in rows], batch_size):
        response = meili_request(
            "POST",
            f"{base}/indexes/{index_uid}/documents?primaryKey=id",
            master_key,
            data=json.dumps(chunk, ensure_ascii=False, default=str).encode("utf-8"),
        )
        task_uids.append(meili_task_uid(response))
        count += len(chunk)
    wait_meili_tasks(base, master_key, task_uids)
    return count


def delete_search_rows(document_ids: list[Any]) -> None:
    if not document_ids:
        return
    with connect(admin=False) as conn:
        conn.execute(
            "DELETE FROM corpus.document_search_index WHERE document_id = ANY(%s::uuid[])",
            (document_ids,),
        )


def delete_meili_documents(
    document_ids: list[Any],
    base_url: str,
    master_key: str,
    index_uid: str,
    batch_size: int,
) -> int:
    if not document_ids:
        return 0
    base = base_url.rstrip("/")
    count = 0
    task_uids: list[int | None] = []
    for chunk in batched([{"id": str(document_id)} for document_id in document_ids], batch_size):
        response = meili_request(
            "POST",
            f"{base}/indexes/{index_uid}/documents/delete-batch",
            master_key,
            data=json.dumps(chunk, ensure_ascii=False).encode("utf-8"),
        )
        task_uids.append(meili_task_uid(response))
        count += len(chunk)
    wait_meili_tasks(base, master_key, task_uids)
    return count


def enqueue_missing_documents(limit: int) -> int:
    limit_clause = "" if limit <= 0 else "LIMIT %(limit)s"
    sql = f"""
      WITH candidates AS (
        SELECT d.id
        FROM corpus.documents d
        LEFT JOIN corpus.document_search_index s ON s.document_id = d.id
        LEFT JOIN ops.document_index_queue q ON q.document_id = d.id
        WHERE s.document_id IS NULL
          AND q.document_id IS NULL
        ORDER BY d.published_at NULLS LAST, d.id
        {limit_clause}
      )
      INSERT INTO ops.document_index_queue (
        document_id, operation, status, priority, dirty_at, available_at, metadata
      )
      SELECT id, 'upsert', 'pending', 500, now(), now(),
             jsonb_build_object('reason', 'search_backfill_missing')
      FROM candidates
      ON CONFLICT (document_id) DO NOTHING
      RETURNING document_id
    """
    with connect(admin=False) as conn:
        # This anti-join can scan the whole document corpus during a backfill.
        # Keep it single-process so it remains safe before shm_size is applied.
        conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        rows = conn.execute(sql, {"limit": limit} if limit > 0 else {}).fetchall()
    return len(rows)


def claim_index_queue(batch_size: int, max_retries: int) -> list[dict[str, Any]]:
    with connect(admin=False) as conn:
        rows = conn.execute(
            """
            WITH candidate AS (
              SELECT document_id
              FROM ops.document_index_queue
              WHERE status = 'pending'
                AND available_at <= now()
                AND retry_count < %s
              ORDER BY priority, dirty_at, document_id
              FOR UPDATE SKIP LOCKED
              LIMIT %s
            )
            UPDATE ops.document_index_queue q
            SET status = 'running',
                claimed_version = q.version,
                claimed_at = now(),
                last_error = NULL
            FROM candidate
            WHERE q.document_id = candidate.document_id
            RETURNING q.document_id, q.operation, q.claimed_version
            """,
            (max_retries, batch_size),
        ).fetchall()
    return list(rows)


def recover_stale_index_claims(claim_timeout_seconds: int) -> int:
    """Return jobs abandoned by a stopped worker to the durable pending state."""
    with connect(admin=False) as conn:
        rows = conn.execute(
            """
            UPDATE ops.document_index_queue
            SET status = 'pending',
                claimed_at = NULL,
                claimed_version = NULL,
                available_at = now(),
                last_error = COALESCE(last_error, 'index worker claim timed out')
            WHERE status = 'running'
              AND claimed_at < now() - make_interval(secs => %s)
            RETURNING document_id
            """,
            (claim_timeout_seconds,),
        ).fetchall()
    return len(rows)


def mark_index_queue_ok(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    with connect(admin=False) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                UPDATE ops.document_index_queue
                SET status = CASE WHEN version = %s THEN 'ok' ELSE 'pending' END,
                    indexed_at = CASE WHEN version = %s THEN now() ELSE indexed_at END,
                    available_at = CASE WHEN version = %s THEN available_at ELSE now() END,
                    claimed_at = NULL,
                    claimed_version = NULL,
                    retry_count = CASE WHEN version = %s THEN 0 ELSE retry_count END,
                    last_error = NULL
                WHERE document_id = %s
                  AND status = 'running'
                """,
                [
                    (
                        item["claimed_version"],
                        item["claimed_version"],
                        item["claimed_version"],
                        item["claimed_version"],
                        item["document_id"],
                    )
                    for item in items
                ],
            )


def mark_index_queue_failed(items: list[dict[str, Any]], error: str, max_retries: int) -> None:
    if not items:
        return
    with connect(admin=False) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                UPDATE ops.document_index_queue
                SET status = CASE
                      WHEN version <> %s THEN 'pending'
                      WHEN retry_count + 1 >= %s THEN 'failed'
                      ELSE 'pending'
                    END,
                    retry_count = CASE
                      WHEN version <> %s THEN retry_count
                      ELSE retry_count + 1
                    END,
                    available_at = CASE
                      WHEN version <> %s THEN now()
                      ELSE now() + make_interval(secs => LEAST(3600, 30 * (2 ^ retry_count)::integer))
                    END,
                    claimed_at = NULL,
                    claimed_version = NULL,
                    last_error = %s
                WHERE document_id = %s
                  AND status = 'running'
                """,
                [
                    (
                        item["claimed_version"],
                        max_retries,
                        item["claimed_version"],
                        item["claimed_version"],
                        error[:4000],
                        item["document_id"],
                    )
                    for item in items
                ],
            )


def process_index_queue(
    limit: int,
    batch_size: int,
    max_retries: int,
    claim_timeout_seconds: int,
    body_chars: int,
    skip_fts: bool,
    skip_meili: bool,
    meili_url: str,
    master_key: str,
    index_uid: str,
    meili_batch_size: int,
    meili_body_chars: int,
) -> tuple[int, int]:
    processed = 0
    meili_total = 0
    recovered = recover_stale_index_claims(claim_timeout_seconds)
    if recovered:
        print(f"recovered stale queue claims={recovered}")
    while not limit or processed < limit:
        size = min(batch_size, limit - processed) if limit else batch_size
        items = claim_index_queue(max(1, size), max_retries)
        if not items:
            break
        try:
            delete_items = [item for item in items if item["operation"] == "delete"]
            upsert_items = [item for item in items if item["operation"] == "upsert"]
            delete_ids = [item["document_id"] for item in delete_items]
            upsert_ids = [item["document_id"] for item in upsert_items]
            if delete_ids:
                if not skip_fts:
                    delete_search_rows(delete_ids)
                if not skip_meili:
                    meili_total += delete_meili_documents(
                        delete_ids, meili_url, master_key, index_uid, meili_batch_size
                    )

            if upsert_ids:
                raw_rows = fetch_documents(
                    since=None,
                    until=None,
                    limit=len(upsert_ids),
                    offset=0,
                    body_chars=body_chars,
                    missing_only=False,
                document_ids=upsert_ids,
                cursor_published_at=None,
                cursor_document_id=None,
                )
                rows = [build_search_row(row, body_chars) for row in raw_rows]
                returned_ids = {row["document_id"] for row in rows}
                missing_ids = [document_id for document_id in upsert_ids if document_id not in returned_ids]
                if rows:
                    if not skip_fts:
                        upsert_fts(rows)
                    if not skip_meili:
                        meili_total += upsert_meili(
                            rows,
                            meili_url,
                            master_key,
                            index_uid,
                            meili_batch_size,
                            meili_body_chars,
                        )
                if missing_ids:
                    if not skip_fts:
                        delete_search_rows(missing_ids)
                    if not skip_meili:
                        meili_total += delete_meili_documents(
                            missing_ids, meili_url, master_key, index_uid, meili_batch_size
                        )
            mark_index_queue_ok(items)
            processed += len(items)
            print(f"queue batch rows={len(items)} total={processed} meili_total={meili_total}")
        except Exception as exc:  # noqa: BLE001
            mark_index_queue_failed(items, str(exc), max_retries)
            raise
    return processed, meili_total


def apply_schema() -> None:
    raw_sql = (ROOT / "docker/postgres/schema_document_search.sql").read_text(encoding="utf-8")
    sql = "\n".join(line for line in raw_sql.splitlines() if not line.lstrip().startswith("\\"))
    with connect(admin=True) as conn:
        conn.execute(sql)


def parse_date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync finance document search indexes into PostgreSQL FTS and Meilisearch.")
    parser.add_argument("--since", help="Published date lower bound YYYY-MM-DD.")
    parser.add_argument("--until", help="Published date upper bound YYYY-MM-DD.")
    parser.add_argument("--days", type=int, help="Use today-days+1 as --since when --since is omitted.")
    parser.add_argument("--limit", type=int, default=0, help="Max documents to sync. 0 means no cap.")
    parser.add_argument("--fetch-size", type=int, default=1000)
    parser.add_argument("--meili-batch-size", type=int, default=500)
    parser.add_argument("--meili-body-chars", type=int, default=DEFAULT_MEILI_BODY_CHARS)
    parser.add_argument("--body-chars", type=int, default=DEFAULT_BODY_CHARS)
    parser.add_argument("--skip-schema", action="store_true")
    parser.add_argument("--skip-fts", action="store_true")
    parser.add_argument("--skip-meili", action="store_true")
    parser.add_argument("--missing-only", action="store_true", help="Only sync documents not yet present in corpus.document_search_index.")
    parser.add_argument("--enqueue-missing", action="store_true", help="Queue documents missing from the search index for durable backfill.")
    parser.add_argument("--enqueue-limit", type=int, default=0, help="Maximum missing documents to enqueue. 0 means all.")
    parser.add_argument("--queue", action="store_true", help="Process the durable document-index queue instead of a date scan.")
    parser.add_argument("--queue-limit", type=int, default=0, help="Maximum queued documents to process. 0 means until empty.")
    parser.add_argument(
        "--queue-batch-size",
        type=int,
        default=50,
        help="Documents per durable-queue transaction (default: 50).",
    )
    parser.add_argument("--max-retries", type=int, default=5)
    parser.add_argument(
        "--claim-timeout-seconds",
        type=int,
        default=1800,
        help="Return queue jobs abandoned by a stopped worker after this many seconds.",
    )
    parser.add_argument("--meili-url", default=os.getenv("MEILI_URL", "http://127.0.0.1:17700"))
    parser.add_argument("--index", default=os.getenv("MEILI_INDEX", DEFAULT_INDEX))
    args = parser.parse_args()

    since = parse_date(args.since)
    until = parse_date(args.until)
    if since is None and args.days:
        since = today() - dt.timedelta(days=max(args.days, 1) - 1)
    env = load_env()
    master_key = os.getenv("MEILI_MASTER_KEY", env.get("MEILI_MASTER_KEY", ""))

    if not args.skip_schema:
        apply_schema()
    if args.enqueue_missing or args.queue:
        ensure_ops_schema()
    if not args.skip_meili:
        if not master_key:
            raise SystemExit("missing MEILI_MASTER_KEY")
        ensure_meili_index(args.meili_url, master_key, args.index)

    if args.enqueue_missing:
        queued = enqueue_missing_documents(max(0, args.enqueue_limit))
        print(f"document_search_enqueue_missing queued={queued}")

    if args.queue:
        total, meili_total = process_index_queue(
            limit=max(0, args.queue_limit),
            batch_size=max(1, args.queue_batch_size),
            max_retries=max(1, args.max_retries),
            claim_timeout_seconds=max(1, args.claim_timeout_seconds),
            body_chars=max(1, args.body_chars),
            skip_fts=args.skip_fts,
            skip_meili=args.skip_meili,
            meili_url=args.meili_url,
            master_key=master_key,
            index_uid=args.index,
            meili_batch_size=max(1, args.meili_batch_size),
            meili_body_chars=max(0, args.meili_body_chars),
        )
        print(f"document_search_queue complete rows={total} meili_rows={meili_total}")
        return 0

    total = 0
    meili_total = 0
    offset = 0
    cursor_published_at = None
    cursor_document_id = None
    while True:
        remaining = args.limit - total if args.limit else args.fetch_size
        if args.limit and remaining <= 0:
            break
        fetch_size = min(args.fetch_size, remaining) if args.limit else args.fetch_size
        raw_rows = fetch_documents(
            since,
            until,
            fetch_size,
            offset,
            args.body_chars,
            args.missing_only,
            cursor_published_at=None if args.missing_only else cursor_published_at,
            cursor_document_id=None if args.missing_only else cursor_document_id,
        )
        if not raw_rows:
            break
        rows = [build_search_row(row, args.body_chars) for row in raw_rows]
        if not args.skip_fts:
            upsert_fts(rows)
        if not args.skip_meili:
            meili_total += upsert_meili(
                rows,
                args.meili_url,
                master_key,
                args.index,
                args.meili_batch_size,
                max(0, args.meili_body_chars),
            )
        total += len(rows)
        if not args.missing_only:
            last = raw_rows[-1]
            cursor_published_at = last.get("published_at")
            cursor_document_id = last["id"]
        print(f"synced batch rows={len(rows)} total={total} meili_total={meili_total}")
        if len(raw_rows) < fetch_size:
            break
        time.sleep(0.05)

    print(f"document_search_sync complete rows={total} meili_rows={meili_total} since={since} until={until}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
