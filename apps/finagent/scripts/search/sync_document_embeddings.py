#!/usr/bin/env python3
"""Incrementally chunk finance documents and build local Chinese embeddings."""

from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from fastembed import TextEmbedding
from psycopg.rows import dict_row
import requests


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts/pipeline"))
from inference_http import InferenceCancelled, post_with_capacity_wait

from common import connect as pipeline_connect  # noqa: E402


EMBEDDING_PROVIDER = os.getenv("FINANCE_EMBEDDING_PROVIDER", "local").strip().lower()
MODEL_NAME = os.getenv("FINANCE_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5").strip()
MODEL_DIMENSIONS = int(os.getenv("FINANCE_EMBEDDING_DIMENSIONS", "512"))
EMBEDDING_BASE_URL = os.getenv(
    "FINANCE_EMBEDDING_BASE_URL",
    os.getenv("FINANCE_API_BASE_URL", "http://127.0.0.1:8082/v1"),
).rstrip("/")
EMBEDDING_API_KEY_ENV = os.getenv("FINANCE_EMBEDDING_API_KEY_ENV", "FINANCE_API_KEY")
EMBEDDING_TABLE = (
    "corpus.document_embeddings_v2" if MODEL_DIMENSIONS == 1024
    else "corpus.document_embeddings"
)
DEFAULT_CACHE_DIR = ROOT.parent / "data/models/fastembed"
SENTENCE_BOUNDARY_RE = re.compile(r"[。！？!?；;\n]")
BLOCK_BOUNDARY_RE = re.compile(r"\n{2,}|(?=^#{1,6}\s)|(?=^第\s*\d+\s*页)", re.M)


def connect(admin: bool = False):
    last_error: Exception | None = None
    for attempt in range(6):
        try:
            return pipeline_connect(admin=admin)
        except Exception as exc:
            last_error = exc
            if attempt < 5:
                time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise last_error


def clean_text(value: Any) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def estimate_tokens(text: str) -> int:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_words = len(re.findall(r"[A-Za-z0-9_]+", text))
    punctuation = len(re.findall(r"[^\w\s\u3400-\u9fff]", text))
    return max(1, chinese + latin_words + punctuation // 2)


def chunk_text(title: str, body: str, max_chars: int, overlap_chars: int, max_chunks: int, *, require_complete: bool = False) -> list[str]:
    title = clean_text(title)
    body = clean_text(body)
    text = body or title
    if not text:
        return []
    prefix = f"{title}\n" if title and title not in text[: max(80, len(title) + 10)] else ""
    if max_chars <= len(prefix) or overlap_chars < 0 or max_chunks < 1:
        raise ValueError('invalid embedding window or title exceeds available window')
    # Preserve document headings, page markers, and paragraphs before applying
    # the bounded sentence window. Newlines remain searchable evidence instead
    # of being flattened into arbitrary fixed-width chunks.
    blocks = [clean_text(block) for block in BLOCK_BOUNDARY_RE.split(text) if clean_text(block)]
    text = "\n\n".join(blocks) if blocks else text
    chunks: list[str] = []
    start = 0
    end = 0
    while start < len(text) and len(chunks) < max_chunks:
        hard_end = min(len(text), start + max_chars - len(prefix))
        end = hard_end
        if hard_end < len(text):
            search_start = start + int((hard_end - start) * 0.65)
            boundaries = [m.end() for m in SENTENCE_BOUNDARY_RE.finditer(text, search_start, hard_end)]
            if boundaries:
                end = boundaries[-1]
        content = clean_text(text[start:end])
        if content:
            chunks.append((prefix + content)[:max_chars])
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap_chars)
        start = next_start
    if require_complete and end < len(text):
        raise ValueError('chunk limit leaves source text uncovered')
    return chunks


def vector_literal(values: Any) -> str:
    return "[" + ",".join(f"{float(value):.8g}" for value in values) + "]"


class EmbeddingBudgetExhausted(Exception): pass
class SourceChanged(Exception): pass


def remote_embed(texts: list[str], batch_size: int, deadline: float | None = None,
                 stop_requested=None) -> list[list[float]]:
    api_key = os.getenv(EMBEDDING_API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"missing embedding API key environment variable: {EMBEDDING_API_KEY_ENV}")
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        if deadline is not None and time.monotonic()>=deadline:
            raise EmbeddingBudgetExhausted()
        batch = texts[start:start + batch_size]
        response = post_with_capacity_wait(requests,
            EMBEDDING_BASE_URL + "/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": MODEL_NAME, "input": batch, "encoding_format": "float"},
            timeout=480, deadline=deadline, stop_requested=stop_requested,
        )
        response.raise_for_status()
        data = sorted(response.json().get("data") or [], key=lambda item: item.get("index", 0))
        if len(data) != len(batch) or [item.get('index') for item in data] != list(range(len(batch))):
            raise RuntimeError("embedding API returned missing or duplicate item indexes")
        vectors.extend(item["embedding"] for item in data)
    return vectors


def claim_documents(limit: int, source_types: list[str], max_retries: int, existing_only: bool = False) -> list[dict[str, Any]]:
    with connect(admin=False) as conn:
        conn.row_factory = dict_row
        conn.execute(
            """
            UPDATE ops.document_embedding_queue
            SET status='pending', started_at=NULL, last_error=coalesce(last_error,'') || ' [recovered stale running job]'
            WHERE status='running' AND started_at < now() - interval '2 hours'
            """
        )
        rows = conn.execute(
            f"""
            WITH picked AS (
              SELECT q.document_id
              FROM ops.document_embedding_queue q
              JOIN corpus.documents d ON d.id=q.document_id
              LEFT JOIN ops.document_embedding_model_state ms ON ms.document_id=q.document_id AND ms.embedding_model=%(model_name)s
              WHERE q.status <> 'running' AND (
                  q.status IN ('pending','failed')
                  OR ms.document_id IS NULL
                  OR (ms.document_id IS NOT NULL AND ms.source_updated_at IS DISTINCT FROM q.source_updated_at)
                  OR NOT EXISTS (
                    SELECT 1 FROM {EMBEDDING_TABLE} e
                    WHERE e.document_id=q.document_id AND e.embedding_model=%(model_name)s
                  )
                )
                AND (coalesce(ms.retry_count,0)<%(max_retries)s OR ms.source_updated_at IS DISTINCT FROM q.source_updated_at)
                AND (%(source_types)s::text[]='{{}}'::text[] OR d.source_type=ANY(%(source_types)s::text[]))
                AND (d.source_type <> 'announcement' OR length(coalesce(nullif(d.content_text,''),nullif(d.content_md,''),d.raw_text,'')) >= 200)
                AND (%(existing_only)s=false OR EXISTS(SELECT 1 FROM {EMBEDDING_TABLE} e
                    WHERE e.document_id=d.id AND e.embedding_model=%(model_name)s))
                AND NOT EXISTS (
                  SELECT 1 FROM corpus.document_quarantine x
                  WHERE x.document_id=d.id AND x.released_at IS NULL
                )
              ORDER BY (EXISTS (SELECT 1 FROM agent.watchlist_items wi
                    WHERE wi.removed_at IS NULL AND (wi.stock_code=d.metadata->>'stock_code'
                      OR EXISTS (SELECT 1 FROM research.reports_resolved rr WHERE rr.document_id=d.id AND rr.stock_code=wi.stock_code)))
                    AND d.published_at>now()-interval '90 days') DESC,
                   d.published_at DESC NULLS LAST,q.dirty_at,q.document_id
              FOR UPDATE OF q SKIP LOCKED
              LIMIT %(limit)s
            )
            UPDATE ops.document_embedding_queue q
            SET status='running', started_at=now(), last_error=NULL
            FROM picked p
            WHERE q.document_id=p.document_id
            RETURNING q.document_id, q.source_updated_at
            """,
            {
                "limit": limit, "source_types": source_types,
                "max_retries": max_retries, "model_name": MODEL_NAME,"existing_only":existing_only,
            },
        ).fetchall()
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO ops.document_embedding_model_state(document_id,embedding_model,source_updated_at,status)
                VALUES(%s,%s,%s,'running') ON CONFLICT(document_id,embedding_model) DO UPDATE SET
                retry_count=CASE WHEN ops.document_embedding_model_state.source_updated_at IS DISTINCT FROM excluded.source_updated_at THEN 0 ELSE ops.document_embedding_model_state.retry_count END,
                source_updated_at=excluded.source_updated_at,status='running',updated_at=now()""",
                [(r['document_id'],MODEL_NAME,r['source_updated_at']) for r in rows])
        conn.commit()
        return list(rows)


def fetch_document(document_id: Any) -> dict[str, Any] | None:
    with connect(admin=False) as conn:
        conn.row_factory = dict_row
        return conn.execute(
            """
            SELECT id, source_type, source_name, published_at, title, updated_at,
                   coalesce(nullif(content_text,''), nullif(content_md,''), raw_text, '') AS body
            FROM corpus.documents WHERE id=%s
            """,
            (document_id,),
        ).fetchone()


def reuse_unchanged_vectors(row: dict[str, Any], chunks: list[str]) -> bool:
    """Revalidate metadata-only changes without recomputing unchanged content.

    Compare the entire ordered chunk sequence as well as every content hash;
    this also invalidates reuse after a chunker or chunk-budget change.
    """
    if not chunks:
        return False
    digest = hashlib.sha256((clean_text(row.get('title'))+'\n'+clean_text(row.get('body'))).encode()).hexdigest()
    with connect() as conn:
        conn.row_factory = dict_row
        current = conn.execute('SELECT updated_at FROM corpus.documents WHERE id=%s FOR UPDATE',(row['id'],)).fetchone()
        if not current or current['updated_at'] != row['updated_at']:
            raise SourceChanged('document changed before vector reuse')
        old = conn.execute(f"""SELECT c.chunk_text,e.metadata->>'content_sha256' AS digest
          FROM {EMBEDDING_TABLE} e JOIN corpus.document_chunks c ON c.id=e.chunk_id
          WHERE e.document_id=%s AND e.embedding_model=%s ORDER BY c.chunk_index""",
          (row['id'],MODEL_NAME)).fetchall()
        if [r['chunk_text'] for r in old] != chunks or any(r['digest'] != digest for r in old):
            return False
        conn.execute("""UPDATE ops.document_embedding_model_state SET status='ok',
          source_updated_at=%s,content_sha256=%s,retry_count=0,last_error=NULL,updated_at=now()
          WHERE document_id=%s AND embedding_model=%s""",(row['updated_at'],digest,row['id'],MODEL_NAME))
        conn.execute("""UPDATE ops.document_embedding_queue SET status='ok',source_updated_at=%s,
          processed_at=now(),embedding_model=%s,chunk_count=%s,retry_count=0,last_error=NULL
          WHERE document_id=%s""",(row['updated_at'],MODEL_NAME,len(chunks),row['id']))
        conn.commit()
    return True


def store_document(row: dict[str, Any], chunks: list[str], vectors: list[Any], truncated: bool) -> None:
    if len(chunks) != len(vectors):
        raise ValueError('chunk/vector count mismatch')
    content_hash = hashlib.sha256((clean_text(row.get("title")) + "\n" + clean_text(row.get("body"))).encode("utf-8")).hexdigest()
    with connect(admin=False) as conn:
        conn.row_factory = dict_row
        # Serialize replacements for a document, and retain vectors belonging
        # to other models. Deleting all chunks would cascade into the migrated
        # BGE index when building the Qwen index.
        current=conn.execute("SELECT id,updated_at FROM corpus.documents WHERE id=%s FOR UPDATE", (row["id"],)).fetchone()
        if not current or current['updated_at']!=row['updated_at']:
            raise SourceChanged('document changed during embedding; retry latest version')
        conn.execute(
            f"DELETE FROM {EMBEDDING_TABLE} WHERE document_id=%s AND embedding_model=%s",
            (row["id"], MODEL_NAME),
        )
        conn.execute(
            """
            DELETE FROM corpus.document_chunks c
            WHERE c.document_id=%s
              AND NOT EXISTS (SELECT 1 FROM corpus.document_embeddings e WHERE e.chunk_id=c.id)
              AND NOT EXISTS (SELECT 1 FROM corpus.document_embeddings_v2 e WHERE e.chunk_id=c.id)
            """,
            (row["id"],),
        )
        conn.execute("""INSERT INTO ops.document_embedding_model_state(document_id,embedding_model,source_updated_at,status,content_sha256)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(document_id,embedding_model) DO UPDATE SET
            source_updated_at=excluded.source_updated_at,status=excluded.status,content_sha256=excluded.content_sha256,
            retry_count=0,last_error=NULL,updated_at=now()""",
            (row['id'],MODEL_NAME,row['updated_at'],'ok' if chunks else 'skipped',content_hash))
        first_index = conn.execute(
            "SELECT coalesce(max(chunk_index)+1,0) AS first_index FROM corpus.document_chunks WHERE document_id=%s",
            (row["id"],),
        ).fetchone()["first_index"]
        for index, (text, vector) in enumerate(zip(chunks, vectors)):
            chunk = conn.execute(
                """
                INSERT INTO corpus.document_chunks (
                  document_id, chunk_index, chunk_text, token_count, char_count, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)
                RETURNING id
                """,
                (
                    row["id"], first_index + index, text, estimate_tokens(text), len(text),
                    json.dumps({
                        "chunker": "finance_heading_sentence_window_v2", "source_type": row.get("source_type"),
                        "source_updated_at": str(row.get("updated_at")), "content_sha256": content_hash,
                        "document_truncated": truncated,
                    }, ensure_ascii=False),
                ),
            ).fetchone()
            conn.execute(
                f"""
                INSERT INTO {EMBEDDING_TABLE} (
                  document_id, chunk_id, embedding_model, embedding, dimensions, metadata
                ) VALUES (%s,%s,%s,%s::vector,%s,%s::jsonb)
                ON CONFLICT (document_id, chunk_id, embedding_model) DO UPDATE SET
                  embedding=EXCLUDED.embedding, dimensions=EXCLUDED.dimensions,
                  metadata=EXCLUDED.metadata, created_at=now()
                """,
                (
                    row["id"], chunk["id"], MODEL_NAME, vector_literal(vector), MODEL_DIMENSIONS,
                    json.dumps({"normalized": True, "distance": "cosine", "content_sha256": content_hash}),
                ),
            )
        conn.execute(
            """
            UPDATE ops.document_embedding_queue
            SET status=%s, processed_at=now(), embedding_model=%s, chunk_count=%s,
                retry_count=0, last_error=NULL
            WHERE document_id=%s
            """,
            ("ok" if chunks else "skipped", MODEL_NAME, len(chunks), row["id"]),
        )
        conn.execute(
            """
            UPDATE corpus.document_quality
            SET checks = checks || jsonb_build_object(
                  'embedding_chunker','finance_heading_sentence_window_v2',
                  'embedding_chunk_count',%s,
                  'embedding_truncated',%s
                ),
                checked_at=greatest(checked_at,now())
            WHERE document_id=%s
            """,
            (len(chunks), truncated, row["id"]),
        )
        conn.commit()


def refresh_embedding_watermark() -> None:
    with connect(admin=False) as conn:
        row = conn.execute(
            f"""
            WITH eligible AS (
              SELECT d.id,d.updated_at FROM corpus.documents d
              LEFT JOIN corpus.document_quality q ON q.document_id=d.id
              WHERE coalesce(q.quality_status,'accepted') IN ('accepted','short')
                AND NOT EXISTS (SELECT 1 FROM corpus.document_quarantine x WHERE x.document_id=d.id AND x.released_at IS NULL)
            ), embedded AS (
              SELECT d.id,d.updated_at FROM eligible d
              WHERE EXISTS (SELECT 1 FROM {EMBEDDING_TABLE} e WHERE e.document_id=d.id AND e.embedding_model=%s)
            )
            SELECT (SELECT count(*) FROM eligible)::bigint eligible_documents,
              (SELECT count(*) FROM embedded)::bigint embedded_documents,
              (SELECT count(*) FROM embedded d JOIN ops.document_embedding_model_state s ON s.document_id=d.id
                 WHERE s.embedding_model=%s AND s.status='ok' AND s.source_updated_at=d.updated_at)::bigint verified_current_documents,
              count(*)::bigint queue_documents,
              count(*) FILTER (WHERE status='pending')::bigint pending_documents,
              count(*) FILTER (WHERE status='failed' AND embedding_model=%s)::bigint failed_documents,
              max(processed_at) FILTER (WHERE status='ok' AND embedding_model=%s) latest_embedded_at
            FROM ops.document_embedding_queue
            """,
            (MODEL_NAME,MODEL_NAME,MODEL_NAME,MODEL_NAME),
        ).fetchone()
        total = int(row["queue_documents"] or 0)
        eligible = int(row["eligible_documents"] or 0)
        embedded = int(row["embedded_documents"] or 0)
        current = int(row["verified_current_documents"] or 0)
        coverage = 100.0 * current / eligible if eligible else 0.0
        any_version_coverage = 100.0 * embedded / eligible if eligible else 0.0
        quality = "pass" if coverage >= 99.0 and int(row["failed_documents"] or 0) == 0 else "warn"
        conn.execute(
            """
            INSERT INTO analytics.dataset_coverage_latest (
              dataset_key,partition_key,data_through,row_count,entity_count,
              eligible_count,coverage_pct,quality_status,details,refreshed_at
            ) VALUES (
              'corpus.document_embeddings','default',%s,%s,%s,%s,%s,%s,%s::jsonb,now()
            )
            ON CONFLICT (dataset_key,partition_key) DO UPDATE SET
              data_through=EXCLUDED.data_through,row_count=EXCLUDED.row_count,
              entity_count=EXCLUDED.entity_count,eligible_count=EXCLUDED.eligible_count,
              coverage_pct=EXCLUDED.coverage_pct,quality_status=EXCLUDED.quality_status,
              details=EXCLUDED.details,refreshed_at=now()
            """,
            (
                row["latest_embedded_at"],embedded,embedded,eligible,coverage,quality,
                json.dumps({
                    "embedding_model":MODEL_NAME,"model_dimensions":MODEL_DIMENSIONS,"queue_documents": total,"eligible_documents": eligible,
                    "embedded_documents": embedded, "verified_current_documents": current,
                    "model_any_version_coverage_pct":any_version_coverage,
                    "unverified_or_stale_documents":embedded-current,
                    "coverage_definition":"eligible documents with this model and matching source content version",
                    "pending_documents": int(row["pending_documents"] or 0),
                    "failed_documents": int(row["failed_documents"] or 0),
                }),
            ),
        )
        conn.execute(
            """
            INSERT INTO ops.dataset_watermarks (
              dataset_key,partition_key,expected_date,data_through,row_count,
              eligible_count,coverage_pct,quality_status,schema_version,
              source_version,completed_at,details
            ) VALUES (
              'corpus.document_embeddings','default',(now() AT TIME ZONE 'Asia/Shanghai')::date,%s,%s,%s,%s,%s,
              'v3',%s,now(),%s::jsonb
            )
            ON CONFLICT (dataset_key,partition_key) DO UPDATE SET
              expected_date=EXCLUDED.expected_date,data_through=EXCLUDED.data_through,
              row_count=EXCLUDED.row_count,eligible_count=EXCLUDED.eligible_count,
              coverage_pct=EXCLUDED.coverage_pct,quality_status=EXCLUDED.quality_status,
              schema_version=EXCLUDED.schema_version,source_version=EXCLUDED.source_version,
              completed_at=now(),details=EXCLUDED.details
            """,
            (
                row["latest_embedded_at"],embedded,eligible,coverage,quality,
                f"{MODEL_NAME}:hnsw:v3",
                json.dumps({
                    "embedding_model":MODEL_NAME,"model_dimensions":MODEL_DIMENSIONS,"queue_documents": total,"eligible_documents": eligible,
                    "embedded_documents": embedded, "verified_current_documents": current,
                    "model_any_version_coverage_pct":any_version_coverage,
                    "unverified_or_stale_documents":embedded-current,
                    "coverage_definition":"eligible documents with this model and matching source content version",
                    "pending_documents": int(row["pending_documents"] or 0),
                    "failed_documents": int(row["failed_documents"] or 0),
                }),
            ),
        )


def mark_failed(document_id: Any, exc: Exception) -> None:
    with connect(admin=False) as conn:
        conn.execute(
            """
            UPDATE ops.document_embedding_queue
            SET status='failed',embedding_model=%s,retry_count=retry_count+1,processed_at=now(),
                last_error=%s
            WHERE document_id=%s
            """,
            (MODEL_NAME,f"{type(exc).__name__}: {exc}"[:1000],document_id),
        )
        conn.execute("""UPDATE ops.document_embedding_model_state SET status='failed',retry_count=retry_count+1,
            last_error=%s,updated_at=now() WHERE document_id=%s AND embedding_model=%s""",
            (f"{type(exc).__name__}: {exc}"[:1000],document_id,MODEL_NAME))
        conn.commit()


def service_unavailable(exc: Exception) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return isinstance(exc, requests.HTTPError) and exc.response is not None and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


def release_unprocessed_claims(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    with connect(admin=False) as conn:
        conn.execute(
            """
            UPDATE ops.document_embedding_queue
            SET status='pending',started_at=NULL
            WHERE document_id=ANY(%s) AND status='running'
            """,
            ([item["document_id"] for item in items],),
        )
        conn.execute("""UPDATE ops.document_embedding_model_state SET status='pending',updated_at=now()
            WHERE document_id=ANY(%s) AND embedding_model=%s AND status='running'""",
            ([item['document_id'] for item in items],MODEL_NAME))
        conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-seconds", type=int, default=1200)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--source-type", action="append", default=[])
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--overlap-chars", type=int, default=120)
    parser.add_argument("--max-chunks", type=int, default=200)
    parser.add_argument('--revalidate-only',action='store_true',help='Verify existing chunks/hashes; never call an embedding model. Changed content remains pending.')
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    args = parser.parse_args()

    started=time.monotonic();deadline=started+max(1,args.max_seconds)
    limit = max(1, min(args.limit, 5000))
    max_chars = max(300, min(args.max_chars, 1800))
    overlap = max(0, min(args.overlap_chars, max_chars // 3))
    claimed = claim_documents(limit, args.source_type, max(1, args.max_retries),existing_only=args.revalidate_only)
    if not claimed:
        refresh_embedding_watermark()
        print(json.dumps({
            "claimed": 0, "processed": 0, "skipped": 0, "failed": 0, "chunks": 0,
            "model": MODEL_NAME, "dimensions": MODEL_DIMENSIONS,
        }, ensure_ascii=False))
        return 0
    model = None
    try:
        if args.revalidate_only:
            pass
        elif EMBEDDING_PROVIDER == "local":
            model = TextEmbedding(
                model_name=MODEL_NAME,
                cache_dir=str(args.cache_dir),
                threads=2,
            )
        elif EMBEDDING_PROVIDER != "openai_compatible":
            raise RuntimeError(f"unsupported embedding provider: {EMBEDDING_PROVIDER}")
    except Exception:
        release_unprocessed_claims(claimed)
        raise
    processed = skipped = failed = chunks_total = deferred = reused = 0
    for index, item in enumerate(claimed):
        document_id = item["document_id"]
        if time.monotonic()>=deadline:
            remaining=claimed[index:];release_unprocessed_claims(remaining);deferred+=len(remaining);break
        try:
            row = fetch_document(document_id)
            if not row:
                raise RuntimeError("document disappeared after queue claim")
            chunks = chunk_text(
                row.get("title") or "", row.get("body") or "", max_chars, overlap,
                max(1, min(args.max_chunks, 200)),
            )
            if reuse_unchanged_vectors(row,chunks):
                reused += 1
                processed += 1
                chunks_total += len(chunks)
                continue
            if args.revalidate_only:
                release_unprocessed_claims([item]);deferred+=1
                continue
            if not chunks:
                vectors = []
            elif model is not None:
                vectors = list(model.embed(chunks, batch_size=max(1, min(args.batch_size, 128))))
            else:
                vectors = remote_embed(chunks, max(1, min(args.batch_size, 128)), deadline)
            if any(len(vector) != MODEL_DIMENSIONS for vector in vectors):
                raise RuntimeError("embedding model returned an unexpected dimension")
            body = clean_text(row.get("body")) or clean_text(row.get("title"))
            covered_chars = sum(max(0, len(chunk) - len(clean_text(row.get("title")))) for chunk in chunks)
            truncated = bool(body and len(chunks) >= max(1, min(args.max_chunks, 200)) and covered_chars < len(body))
            store_document(row, chunks, vectors, truncated)
            chunks_total += len(chunks)
            if chunks:
                processed += 1
            else:
                skipped += 1
        except (EmbeddingBudgetExhausted, SourceChanged) as exc:
            if isinstance(exc,EmbeddingBudgetExhausted):
                remaining=claimed[index:];release_unprocessed_claims(remaining);deferred+=len(remaining);break
            release_unprocessed_claims([item]);deferred+=1
        except Exception as exc:
            if time.monotonic()>=deadline:
                remaining=claimed[index:];release_unprocessed_claims(remaining);deferred+=len(remaining);break
            mark_failed(document_id, exc)
            failed += 1
            print(f"[failed] {document_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            if service_unavailable(exc):
                # A GPU/backend outage affects the batch, not every document.
                # Leave unattempted work pending rather than consuming all of
                # its retries against the same unavailable model instance.
                remaining = claimed[index + 1:]
                release_unprocessed_claims(remaining)
                deferred = len(remaining)
                break
    refresh_embedding_watermark()
    print(json.dumps({
        "claimed": len(claimed), "processed": processed, "skipped": skipped,
        "elapsed_seconds":round(time.monotonic()-started,3),"budget_seconds":args.max_seconds,
        "failed": failed, "deferred": deferred, "reused_unchanged": reused, "chunks": chunks_total, "model": MODEL_NAME,
        "dimensions": MODEL_DIMENSIONS,
        "revalidate_only":args.revalidate_only,
    }, ensure_ascii=False))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
