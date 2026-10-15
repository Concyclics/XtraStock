from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import datetime as dt
import json
import math
import os
import re
import statistics
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import requests
from psycopg.rows import dict_row


ALLOWED_SCHEMAS = ("agent", "ai", "analytics", "company", "corpus", "kg", "macro", "market", "news", "ops", "research", "public")
TOKEN_RE = re.compile(r"[A-Za-z0-9_+.#-]+|[\u4e00-\u9fff]+")
CLUSTER_STOP_KEYWORDS = (
    "银行", "证券", "消费", "保险", "地产", "医药", "汽车", "电力", "新能源", "软件",
    "支持", "国务院", "发改委", "调查", "限制",
)


THEME_ALIASES = {
    "AI硬件": ["AI", "人工智能", "算力", "光模块", "CPO", "PCB", "HBM", "存储", "半导体设备", "半导体材料", "服务器电源", "液冷", "先进封装"],
    "AI算力": ["AI", "人工智能", "算力", "光模块", "CPO", "PCB", "HBM", "存储", "半导体设备", "半导体材料", "服务器电源", "液冷", "先进封装"],
    "存储": ["存储", "HBM", "DRAM", "NAND", "内存", "闪存", "先进封装", "测试机", "半导体设备", "电子特气"],
    "存储硬件": ["存储", "HBM", "DRAM", "NAND", "内存", "闪存", "先进封装", "测试机", "半导体设备", "电子特气"],
    "光": ["光模块", "CPO", "LPO", "MPO", "光芯片", "光纤", "硅光", "薄膜铌酸锂"],
    "光模块": ["光模块", "CPO", "LPO", "MPO", "光芯片", "光纤", "硅光", "薄膜铌酸锂"],
    "pcb": ["PCB", "TGV", "玻璃基板", "电子布", "树脂", "HDI", "高多层板"],
    "PCB": ["PCB", "TGV", "玻璃基板", "电子布", "树脂", "HDI", "高多层板"],
    "电源": ["服务器电源", "功率半导体", "超级电容", "MLCC", "SiC", "IGBT", "电源管理"],
    "液冷": ["液冷", "散热", "热管理", "数据中心液冷"],
}


def clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except Exception:
        return default


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except Exception:
        return default


def normalize_return_threshold(value: Any, default: float = 0.2) -> float:
    threshold = abs(as_float(value, default) or default)
    return threshold / 100.0 if threshold > 1 else threshold


def normalize_pct_threshold(value: Any, default: float | None = None) -> float | None:
    threshold = as_float(value, default)
    if threshold is None:
        return None
    return threshold / 100.0 if abs(threshold) > 1 else threshold


def metric_name_from_arg(value: Any) -> str:
    text = str(value or "net_profit_parent_yoy").strip().lower()
    if "revenue" in text or "营收" in text or "收入" in text:
        return "revenue"
    if "deduct" in text or "扣非" in text:
        return "deducted_net_profit"
    return "net_profit_parent"


def price_source_from_adjustment(value: Any) -> tuple[str, str]:
    adjustment = str(value or "qfq").strip().lower()
    if adjustment in {"technical", "asset_technical_daily", "analytics"}:
        return "analytics.asset_technical_daily", "technical"
    if adjustment in {"none", "raw", "不复权"}:
        return "eastmoney:kline:day:none", "none"
    # Event-return views use technical data materialized from finalized
    # canonical bars, including explicit fallback/adjustment boundaries.
    return "analytics.asset_technical_daily", "technical"


def theme_terms(theme: str | None) -> list[str]:
    if not theme:
        return []
    text = str(theme).strip()
    terms = [text]
    for key, aliases in THEME_ALIASES.items():
        if key.lower() in text.lower() or text.lower() in key.lower():
            terms.extend(aliases)
    seen = set()
    result = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result


def query_terms(query: str | None, extra_keywords: Any = None) -> list[str]:
    text = str(query or "").strip()
    parts: list[str] = []
    if text:
        for piece in re_split_terms(text):
            parts.extend(theme_terms(piece) or [piece])
    if isinstance(extra_keywords, str):
        parts.extend(re_split_terms(extra_keywords))
    elif isinstance(extra_keywords, list):
        parts.extend(str(x).strip() for x in extra_keywords if str(x).strip())
    seen = set()
    result = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            result.append(part)
    return result


def expandable_terms(terms: list[str]) -> list[str]:
    result = []
    for term in terms:
        cleaned = str(term or "").strip()
        if not cleaned:
            continue
        if re.fullmatch(r"[A-Za-z0-9+.#-]+", cleaned) and len(cleaned) < 2:
            continue
        if not re.search(r"[A-Za-z0-9]", cleaned) and len(cleaned) < 2:
            continue
        result.append(cleaned)
    return result[:12]


def expand_terms_from_catalog(root: str, terms: list[str], max_terms: int = 20) -> tuple[list[str], list[dict[str, Any]]]:
    """Expand bounded aliases from canonical assets and the local concept graph."""
    candidates = expandable_terms(terms)
    if not candidates:
        return terms, []
    patterns = [f"%{term}%" for term in candidates]
    try:
        with connect(root) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            matches = conn.execute(
                """
                WITH matches AS (
                  SELECT 'concept'::text AS entity_type, concept_id::text AS entity_id,
                         name AS canonical_name, NULL::text AS symbol,
                         aliases,
                         CASE
                           WHEN lower(name) = ANY(%(lower_terms)s) OR lower(normalized_name) = ANY(%(lower_terms)s) THEN 100
                           WHEN EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = ANY(%(lower_terms)s)) THEN 95
                           ELSE 70
                         END AS match_score
                  FROM kg.concepts
                  WHERE status = 'active'
                    AND (
                      name ILIKE ANY(%(patterns)s)
                      OR normalized_name ILIKE ANY(%(patterns)s)
                      OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE a ILIKE ANY(%(patterns)s))
                    )
                  UNION ALL
                  SELECT 'asset', asset_id::text, name, symbol, ARRAY[]::text[],
                         CASE WHEN lower(symbol) = ANY(%(lower_terms)s) THEN 100
                              WHEN lower(name) = ANY(%(lower_terms)s) THEN 98 ELSE 65 END
                  FROM market.assets
                  WHERE symbol ILIKE ANY(%(patterns)s) OR name ILIKE ANY(%(patterns)s)
                )
                SELECT entity_type, entity_id, canonical_name, symbol, aliases, match_score
                FROM matches
                ORDER BY match_score DESC, canonical_name NULLS LAST
                LIMIT 12
                """,
                {
                    "patterns": patterns,
                    "lower_terms": [term.lower() for term in candidates],
                },
            ).fetchall()
    except Exception:
        return terms, []

    expanded = list(terms)
    seen = {str(term).lower() for term in expanded}
    match_rows = []
    for row in matches:
        match_rows.append(dict(row))
        values = [row.get("canonical_name"), row.get("symbol"), *(row.get("aliases") or [])]
        for value in values:
            value = str(value or "").strip()
            if not value or value.lower() in seen:
                continue
            seen.add(value.lower())
            expanded.append(value)
            if len(expanded) >= max_terms:
                return expanded, match_rows
    return expanded, match_rows


def re_split_terms(text: str) -> list[str]:
    import re
    return [x.strip() for x in re.split(r"[\s,，;；|/]+", text) if x.strip()]


def source_types_arg(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return re_split_terms(value)
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def normalize_search_backend(value: Any, allowed: set[str]) -> str:
    backend = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "meili": "meilisearch",
        "fts": "postgres_fts",
        "postgres": "postgres_fts",
        "pg_fts": "postgres_fts",
        "bm25": "local_bm25",
        "local": "local_bm25",
        "sql": "legacy",
    }
    backend = aliases.get(backend, backend)
    return backend if backend in allowed else "auto"


def bm25_query_terms(query: str | None, extra_keywords: Any = None) -> list[str]:
    raw_parts: list[str] = []
    if query:
        raw_parts.extend(re_split_terms(str(query)))
    if isinstance(extra_keywords, str):
        raw_parts.extend(re_split_terms(extra_keywords))
    elif isinstance(extra_keywords, list):
        raw_parts.extend(str(x).strip() for x in extra_keywords if str(x).strip())

    terms: list[str] = []
    for part in raw_parts:
        for token in TOKEN_RE.findall(part):
            if re.fullmatch(r"[\u4e00-\u9fff]+", token):
                if len(token) <= 8:
                    terms.append(token)
                if len(token) >= 3:
                    terms.extend(token[i : i + 2] for i in range(len(token) - 1))
                    terms.extend(token[i : i + 3] for i in range(len(token) - 2))
            else:
                lower = token.lower()
                if len(lower) >= 2:
                    terms.append(lower)

    seen = set()
    result = []
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    return result


def bm25_doc_length(text: str) -> int:
    length = 0
    for token in TOKEN_RE.findall(text or ""):
        if re.fullmatch(r"[\u4e00-\u9fff]+", token):
            length += max(1, len(token) - 1)
        else:
            length += 1
    return max(length, 1)


def weighted_doc_text(row: dict[str, Any]) -> tuple[str, str, str, str]:
    title = str(row.get("title") or "")
    keyword_text = " ".join(str(x) for x in (row.get("keywords") or []) if x)
    body = str(row.get("body") or "")
    weighted = f"{title} {title} {title} {keyword_text} {keyword_text} {body}"
    return title, keyword_text, body, weighted


def best_snippet(body: str, terms: list[str], snippet_chars: int) -> str:
    if not body:
        return ""
    body_lower = body.lower()
    positions = [body_lower.find(term.lower()) for term in terms if term and body_lower.find(term.lower()) >= 0]
    if not positions:
        return body[:snippet_chars]
    start = max(0, min(positions) - snippet_chars // 4)
    return body[start : start + snippet_chars]


def score_bm25_rows(rows: list[dict[str, Any]], terms: list[str], limit: int, snippet_chars: int) -> list[dict[str, Any]]:
    if not rows or not terms:
        return []
    prepared = []
    df = {term: 0 for term in terms}
    for row in rows:
        title, keyword_text, body, weighted = weighted_doc_text(row)
        lower = weighted.lower()
        doc_len = bm25_doc_length(weighted)
        term_freqs = {}
        for term in terms:
            tf = lower.count(term.lower())
            if tf:
                term_freqs[term] = tf
                df[term] += 1
        prepared.append((row, title, keyword_text, body, doc_len, term_freqs))

    avg_len = sum(item[4] for item in prepared) / max(len(prepared), 1)
    n_docs = len(prepared)
    k1 = 1.5
    b = 0.75
    scored = []
    for row, title, keyword_text, body, doc_len, term_freqs in prepared:
        score = 0.0
        matched_terms = []
        for term, tf in term_freqs.items():
            idf = math.log(1 + (n_docs - df[term] + 0.5) / (df[term] + 0.5))
            denom = tf + k1 * (1 - b + b * doc_len / avg_len)
            score += idf * (tf * (k1 + 1)) / denom
            matched_terms.append(term)
        if not matched_terms:
            continue
        title_lower = title.lower()
        keyword_lower = keyword_text.lower()
        title_hits = sum(1 for term in matched_terms if term.lower() in title_lower)
        keyword_hits = sum(1 for term in matched_terms if term.lower() in keyword_lower)
        score += title_hits * 1.2 + keyword_hits * 0.8
        out = {k: v for k, v in row.items() if k != "body"}
        out["bm25_score"] = round(score, 4)
        out["matched_terms"] = matched_terms[:20]
        out["snippet"] = best_snippet(body, matched_terms, snippet_chars)
        scored.append(out)
    scored.sort(key=lambda item: (item.get("bm25_score") or 0, item.get("published_date") or ""), reverse=True)
    return scored[:limit]


def semantic_document_rows(
    root: str,
    query: str,
    source_types: list[str],
    days: int,
    stock_code: str,
    limit: int,
    snippet_chars: int,
    decision_cutoff: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not query.strip():
        return [], {"semantic_used": False, "semantic_reason": "empty_query"}
    try:
        # Tool subprocesses and direct quality checks do not necessarily inherit
        # the service EnvironmentFile. Use the same embedding configuration in
        # both paths; never silently query the retired BGE index on this host.
        from xtrastock.config import load_legacy_env
        configured = load_legacy_env()

        def embedding_option(name: str, default: str = "") -> str:
            return os.getenv(name, configured.get(name, default))

        provider = embedding_option("FINANCE_EMBEDDING_PROVIDER", "local").strip().lower()
        model_name = embedding_option("FINANCE_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5").strip()
        dimensions = int(embedding_option("FINANCE_EMBEDDING_DIMENSIONS", "512"))
        embedding_table = (
            "corpus.document_embeddings_v2" if dimensions == 1024
            else "corpus.document_embeddings"
        )
        if provider == "openai_compatible":
            base_url = embedding_option(
                "FINANCE_EMBEDDING_BASE_URL",
                embedding_option("FINANCE_API_BASE_URL", "http://localhost:8082/v1"),
            ).rstrip("/")
            key_env = embedding_option("FINANCE_EMBEDDING_API_KEY_ENV", "FINANCE_API_KEY")
            api_key = embedding_option(key_env).strip()
            if not api_key:
                raise RuntimeError(f"missing embedding API key: {key_env}")
            response = requests.post(
                base_url + "/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model_name, "input": [query],
                    "encoding_format": "float",
                },
                # The local serverless model can take 25+ seconds to load.
                # Bound connection setup separately and let cold starts finish;
                # the parent retrieval tools have a larger overall deadline.
                timeout=(3, 90),
            )
            response.raise_for_status()
            vectors = [item["embedding"] for item in sorted(
                response.json().get("data") or [], key=lambda item: item.get("index", 0)
            )]
        else:
            from fastembed import TextEmbedding

            cache_dir = os.environ["FINANCE_EMBED_CACHE_DIR"]
            model = TextEmbedding(model_name=model_name, cache_dir=cache_dir, threads=2)
            vectors = list(model.query_embed([query]))
        if not vectors or len(vectors[0]) != dimensions:
            return [], {"semantic_used": False, "semantic_reason": "unexpected_query_vector_dimension"}
        vector = "[" + ",".join(f"{float(value):.8g}" for value in vectors[0]) + "]"
    except Exception as exc:
        return [], {"semantic_used": False, "semantic_reason": f"model_error:{type(exc).__name__}"}

    try:
        with connect(root) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            rows = conn.execute(
                f"""
                WITH bounds AS (
                  SELECT least(max(published_at),%(decision_cutoff)s) AS latest_ts FROM corpus.documents
                ), nearest_chunks AS MATERIALIZED (
                  SELECT e.document_id, e.chunk_id,
                         1 - (e.embedding <=> %(query_vector)s::vector) AS semantic_score
                  FROM {embedding_table} e
                  WHERE e.embedding_model=%(model_name)s
                    AND e.chunk_id IS NOT NULL
                    AND (%(stock_code)s='' OR EXISTS (
                      SELECT 1 FROM corpus.document_search_index si
                      WHERE si.document_id=e.document_id
                        AND si.stock_codes @> ARRAY[%(stock_code)s]::text[]
                    ))
                  ORDER BY e.embedding <=> %(query_vector)s::vector
                  LIMIT %(candidate_limit)s
                ), filtered AS (
                  SELECT n.*,d.source_type,d.source_name,d.published_at,d.title,d.url
                  FROM nearest_chunks n
                  JOIN corpus.documents d ON d.id=n.document_id
                  JOIN ops.document_embedding_model_state ms ON ms.document_id=d.id
                    AND ms.embedding_model=%(model_name)s AND ms.status='ok' AND ms.source_updated_at=d.updated_at
                  CROSS JOIN bounds b
                  WHERE d.published_at IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM corpus.document_quarantine x WHERE x.document_id=d.id AND x.released_at IS NULL)
                    AND d.published_at <= %(decision_cutoff)s
                    AND d.published_at >= b.latest_ts - ((%(days)s::int - 1) * interval '1 day')
                    AND (%(has_sources)s=false OR d.source_type=ANY(%(source_types)s::text[]))
                    AND (%(stock_code)s='' OR EXISTS (
                      SELECT 1 FROM corpus.document_search_index si
                      WHERE si.document_id=d.id
                        AND si.stock_codes @> ARRAY[%(stock_code)s]::text[]
                    ))
                ), nearest AS (
                  SELECT DISTINCT ON (document_id) *
                  FROM filtered
                  ORDER BY document_id,semantic_score DESC,chunk_id
                )
                SELECT n.document_id, n.source_type, n.source_name,
                       n.published_at::date AS published_date, n.title, n.url,
                       n.chunk_id, left(c.chunk_text, %(snippet_chars)s) AS snippet,
                       round(n.semantic_score::numeric,6) AS semantic_score,
                       coalesce(r.stock_code,ds.metadata->>'stock_code') AS stock_code,
                       coalesce(r.stock_name,ds.metadata->>'stock_name') AS stock_name,
                       r.institution, r.rating, r.rating_change, r.industry_name
                FROM nearest n
                JOIN corpus.document_chunks c ON c.id=n.chunk_id
                JOIN corpus.documents ds ON ds.id=n.document_id
                LEFT JOIN LATERAL (
                  SELECT stock_code, stock_name, institution, rating, rating_change, industry_name
                  FROM research.reports_resolved rr WHERE rr.document_id=n.document_id
                  ORDER BY rr.report_date DESC NULLS LAST LIMIT 1
                ) r ON true
                ORDER BY n.semantic_score DESC, n.published_at DESC NULLS LAST
                LIMIT %(limit)s
                """,
                {
                    "query_vector": vector, "model_name": model_name, "days": days,
                    "decision_cutoff": decision_cutoff or dt.datetime.now(dt.timezone.utc),
                    "has_sources": bool(source_types), "source_types": source_types,
                    "stock_code": stock_code, "candidate_limit": max(limit * 30, 600),
                    "snippet_chars": snippet_chars, "limit": max(limit * 3, 30),
                },
            ).fetchall()
            coverage = conn.execute(
                """
                SELECT
                  coalesce((details->>'queue_documents')::int,eligible_count::int,0) AS queue_documents,
                  coalesce((details->>'embedded_documents')::int,row_count::int,0) AS embedded_documents,
                  coalesce((details->>'pending_documents')::int,0) AS pending_documents,
                  coalesce((details->>'failed_documents')::int,0) AS failed_documents,
                  eligible_count,coverage_pct,
                  details->>'embedding_model' AS coverage_model,
                  coalesce((details->>'verified_current_documents')::int,0) AS verified_current_documents,
                  data_through AS latest_embedded_at
                FROM analytics.dataset_coverage_latest
                WHERE dataset_key='corpus.document_embeddings' AND partition_key='default'
                """
            ).fetchone() or {}
    except Exception as exc:
        return [], {"semantic_used": False, "semantic_reason": f"query_error:{type(exc).__name__}"}

    result = []
    for row in rows:
        result.append({
            "id": str(row.get("document_id")),
            "source_type": row.get("source_type"), "source_name": row.get("source_name"),
            "published_date": row.get("published_date"), "title": row.get("title"),
            "url": row.get("url"), "stock_code": row.get("stock_code"),
            "stock_name": row.get("stock_name"), "institution": row.get("institution"),
            "rating": row.get("rating"), "rating_change": row.get("rating_change"),
            "industry_name": row.get("industry_name"), "snippet": row.get("snippet"),
            "semantic_score": float(row.get("semantic_score") or 0),
            "semantic_chunk_id": str(row.get("chunk_id")),
        })
    total = int(coverage.get("queue_documents") or 0)
    embedded = int(coverage.get("embedded_documents") or 0)
    return result, {
        "semantic_used": True, "semantic_hits": len(result), "embedding_model": model_name,
        "embedding_dimensions": dimensions, "semantic_index_documents": embedded,
        "semantic_queue_documents": total,
        "semantic_coverage_pct": float(coverage['coverage_pct']) if coverage.get('coverage_model')==model_name and coverage.get('coverage_pct') is not None else None,
        "semantic_eligible_documents": coverage.get('eligible_count'),
        "semantic_verified_current_documents": coverage.get('verified_current_documents'),
        "semantic_coverage_basis": "eligible documents with current model and matching source content version",
        "semantic_pending_documents": int(coverage.get("pending_documents") or 0),
        "semantic_failed_documents": int(coverage.get("failed_documents") or 0),
        "latest_embedded_at": coverage.get("latest_embedded_at"),
    }


def hybrid_document_rows(
    root: str,
    query: str,
    terms: list[str],
    source_types: list[str],
    days: int,
    stock_code: str,
    limit: int,
    snippet_chars: int,
    decision_cutoff: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    meili_rows, meili_meta = meili_document_rows(
        root, query, terms, source_types, days, stock_code,
        max(limit * 3, 30), snippet_chars, bm25=True,
        decision_cutoff=decision_cutoff,
    )
    # PostgreSQL FTS is the complete lexical source of truth. Meilisearch is a
    # low-latency independent channel, never a condition that suppresses FTS.
    # Keep the user's explicit terms and the first bounded aliases. Sorting by
    # token length used to discard phrases such as CPO/光模块 in favor of remote
    # long aliases, which made FTS return unrelated or empty candidate sets.
    lexical_terms = list(dict.fromkeys(terms))[:8]
    try:
        with connect(root) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            fts_rows, fts_meta = fts_document_rows(
                conn, query, lexical_terms, source_types, days, stock_code,
                max(limit * 3, 30), snippet_chars, bm25=True,
                decision_cutoff=decision_cutoff,
            )
    except Exception as exc:
        fts_rows = []
        fts_meta = {"fts_used": False, "fts_reason": f"query_error:{type(exc).__name__}"}
    semantic_rows, semantic_meta = semantic_document_rows(
        root, query, source_types, days, stock_code, limit, snippet_chars,
        decision_cutoff=decision_cutoff,
    )

    merged: dict[str, dict[str, Any]] = {}
    for channel, rows in (
        ("meilisearch", meili_rows),
        ("postgres_fts", fts_rows),
        ("semantic", semantic_rows),
    ):
        for rank, row in enumerate(rows, start=1):
            document_id = str(row.get("id") or row.get("document_id") or "")
            if not document_id:
                continue
            current = merged.setdefault(document_id, {**row, "retrieval_channels": [], "rrf_score": 0.0})
            current["retrieval_channels"].append(channel)
            current["rrf_score"] += 1.0 / (60 + rank)
            if channel in {"meilisearch", "postgres_fts"}:
                current[f"{channel}_rank"] = rank
                current[f"{channel}_score"] = row.get("bm25_score") or row.get("candidate_score")
                for key, value in row.items():
                    if current.get(key) in (None, "", []):
                        current[key] = value
            else:
                current["semantic_rank"] = rank
                current["semantic_score"] = row.get("semantic_score")
                current["semantic_chunk_id"] = row.get("semantic_chunk_id")
                if row.get("snippet") and (not current.get("snippet") or float(row.get("semantic_score") or 0) >= 0.55):
                    current["snippet"] = row.get("snippet")
                for key, value in row.items():
                    if current.get(key) in (None, "", []):
                        current[key] = value

    lower_terms = [term.lower() for term in terms]
    for row in merged.values():
        title = str(row.get("title") or "").lower()
        exact_title_hits = sum(1 for term in lower_terms if term and term in title)
        semantic_score = max(0.0, float(row.get("semantic_score") or 0))
        row["rerank_score"] = round(row["rrf_score"] + semantic_score * 0.05 + exact_title_hits * 0.01, 6)
        row["rrf_score"] = round(row["rrf_score"], 6)
    ranked = sorted(
        merged.values(),
        key=lambda row: (row.get("rerank_score") or 0, str(row.get("published_date") or "")),
        reverse=True,
    )[:limit]
    active_channels = [
        name for name, rows in (
            ("meilisearch", meili_rows), ("postgres_fts", fts_rows), ("semantic", semantic_rows)
        ) if rows
    ]
    return ranked, {
        **meili_meta, **fts_meta, **semantic_meta,
        "hybrid_used": len(active_channels) >= 2,
        "retrieval_channels": active_channels,
        "fusion": "independent_meili_fts_vector_rrf_k60_plus_semantic_title_rerank_v2",
        "meili_hits": len(meili_rows), "fts_hits": len(fts_rows),
        "lexical_hits": len({str(row.get('id') or row.get('document_id')) for row in meili_rows + fts_rows}),
        "semantic_hits": len(semantic_rows), "fts_terms": lexical_terms,
        "hybrid_candidates": len(merged),
    }


def json_arg(index: int) -> dict[str, Any]:
    if len(sys.argv) <= index or not sys.argv[index]:
        return {}
    return json.loads(sys.argv[index])


def load_env(root: str) -> dict[str, str]:
    from xtrastock.config import load_legacy_env
    return load_legacy_env()


def connect(root: str):
    from xtrastock.config import Settings
    statement_timeout_ms = clamp_int(os.getenv('FINANCE_DB_STATEMENT_TIMEOUT_MS'), 15000, 1000, 20000)
    kwargs = {**Settings().postgres_kwargs('reader'), 'row_factory': dict_row, 'connect_timeout': 2,
              'options': f'-c statement_timeout={statement_timeout_ms} -c lock_timeout=3000 -c idle_in_transaction_session_timeout=20000'}
    last_exc = None
    for attempt in range(3):
        try:
            return psycopg.connect(**kwargs)
        except psycopg.OperationalError as exc:
            last_exc = exc
            if attempt == 2:
                break
            time.sleep(0.25 * (attempt + 1))
    raise last_exc


def meili_config(root: str) -> tuple[str, str, str, bool]:
    env = load_env(root)
    url = os.getenv("MEILI_URL", env.get("MEILI_URL", "http://127.0.0.1:17700")).rstrip("/")
    key = os.getenv("MEILI_MASTER_KEY", env.get("MEILI_MASTER_KEY", ""))
    index = os.getenv("MEILI_INDEX", env.get("MEILI_INDEX", "finance_recent_documents"))
    enabled = os.getenv("FINANCE_MEILI_ENABLED", "1").lower() not in {"0", "false", "no"}
    return url, key, index, enabled


def meili_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def meili_filter(
    source_types: list[str], days: int, stock_code: str,
    decision_cutoff: dt.datetime | None = None,
) -> list[str]:
    filters: list[str] = []
    if source_types:
        filters.append("source_type IN [" + ", ".join(meili_quote(value) for value in source_types) + "]")
    if days > 0:
        cutoff_ts = int((decision_cutoff or dt.datetime.now(dt.timezone.utc)).timestamp())
        since = cutoff_ts - ((days - 1) * 86400)
        filters.append(f"published_at >= {since}")
        filters.append(f"published_at <= {cutoff_ts}")
    if stock_code:
        filters.append(f"stock_codes = {meili_quote(stock_code)}")
    return filters


def first_snippet(text: str, terms: list[str], max_chars: int) -> str:
    body = " ".join(str(text or "").split())
    if not body:
        return ""
    lower = body.lower()
    positions = [lower.find(term.lower()) for term in terms if term and lower.find(term.lower()) >= 0]
    if positions:
        start = max(min(positions) - max_chars // 4, 0)
    else:
        start = 0
    return body[start : start + max_chars]


def meili_document_rows(
    root: str,
    query: str,
    terms: list[str],
    source_types: list[str],
    days: int,
    stock_code: str,
    limit: int,
    snippet_chars: int,
    bm25: bool,
    decision_cutoff: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url, key, index, enabled = meili_config(root)
    if not enabled or not key or not terms:
        return [], {"meili_used": False, "meili_reason": "disabled_or_empty"}
    payload = {
        "q": query or " ".join(terms),
        "limit": limit,
        "filter": meili_filter(source_types, days, stock_code, decision_cutoff),
        "showRankingScore": True,
        "attributesToRetrieve": [
            "id",
            "source_type",
            "source_name",
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
            "body",
        ],
    }
    try:
        response = requests.post(
            f"{url}/indexes/{index}/search",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=5,
        )
        response.raise_for_status()
        hits = response.json().get("hits", [])
    except Exception as exc:
        return [], {"meili_used": False, "meili_reason": f"{type(exc).__name__}: {exc}"[:240]}

    rows: list[dict[str, Any]] = []
    for hit in hits:
        keywords = hit.get("keywords") or []
        matched_keywords = [
            keyword
            for keyword in keywords
            if any(term.lower() in str(keyword).lower() for term in terms)
        ]
        common = {
            "source_type": hit.get("source_type"),
            "source_name": hit.get("source_name"),
            "published_date": hit.get("published_date"),
            "title": hit.get("title"),
            "url": hit.get("url"),
            "stock_code": (hit.get("stock_codes") or [None])[0],
            "stock_name": hit.get("stock_name"),
            "institution": hit.get("institution"),
            "rating": hit.get("rating"),
            "rating_change": hit.get("rating_change"),
            "industry_name": hit.get("industry_name") or ((hit.get("industries") or [None])[0]),
            "matched_keywords": matched_keywords,
            "snippet": first_snippet(hit.get("body") or "", terms, snippet_chars),
        }
        score = hit.get("_rankingScore")
        if bm25:
            rows.append(
                {
                    "id": hit.get("id"),
                    **common,
                    "keywords": keywords,
                    "candidate_score": score,
                    "bm25_score": score,
                    "matched_terms": [term for term in terms if term.lower() in (hit.get("body") or hit.get("title") or "").lower()],
                }
            )
        else:
            rows.append(
                {
                    "document_id": hit.get("id"),
                    **common,
                    "score": score,
                }
            )
    return rows, {"meili_used": True, "meili_index": index, "meili_hits": len(rows)}


def fts_document_rows(
    conn,
    query: str,
    terms: list[str],
    source_types: list[str],
    days: int,
    stock_code: str,
    limit: int,
    snippet_chars: int,
    bm25: bool,
    decision_cutoff: dt.datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not terms:
        return [], {"fts_used": False, "fts_reason": "empty_terms"}
    use_trigram = any(re.search(r"[\u3400-\u9fff]", term) for term in terms)
    # pg_trgm cannot narrow a two-character wildcard. Prefer useful complete
    # terms / trigrams from the original query before applying a body search.
    long_terms = [term for term in terms if len(term)>=3]
    if use_trigram and not long_terms:
        long_terms = [term for term in bm25_query_terms(query,None) if len(term)>=3]
    patterns = [f"%{term}%" for term in (long_terms[:8] or terms[:4])]
    numeric_match = re.fullmatch(r"[+-]?\d+(?:\.\d+)?%?", (query or "").strip())
    numeric_token = numeric_match.group(0).rstrip("%").lstrip("+-") if numeric_match else ""
    numeric_variants = [numeric_token] if numeric_token else []
    numeric_sign = (query or "").strip()[:1]
    if numeric_token:
        integer, dot, fraction = numeric_token.partition('.')
        if len(integer) < 100 and not (len(integer) > 1 and integer.startswith('0')):
            formatted = f'{int(integer):,}' + dot + fraction
            if formatted != numeric_token:
                numeric_variants.append(formatted)
                patterns.append('%' + formatted + '%')
    numeric_patterns = [
        '%' + ('-' if numeric_sign == '-' else '') + value + '%'
        for value in numeric_variants
    ]
    # The LIKE predicates use pg_trgm; the bounded-number check prevents a tail
    # substring such as 123 from being reported as the exact number 9123.
    number_sign_pattern = '-' if numeric_sign == '-' else r'\+?' if numeric_sign == '+' else r'[+-]?'
    numeric_regex = (
        r'(?<![0-9.,+-])' + number_sign_pattern
        + '(?:' + '|'.join(re.escape(value) for value in numeric_variants) + ')'
        + r'(?![0-9]|[,.][0-9])'
    ) if numeric_variants else ''
    exact_patterns = [
        '%' + term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        for term in terms[:8]
    ]
    # Exact numeric/long-phrase matches can use the GIN index immediately.
    # Broad substring ORs used to time out before returning even exact hits.
    # Match both signs for unsigned numbers, as the substring search did; a
    # leading minus must not become websearch's boolean NOT operator.
    prefer_exact = bool(numeric_token) or len((query or "").strip()) > 18

    def fetch_rows(exact_only: bool):
        return conn.execute(
            """
            WITH bounds AS (
              SELECT least(max(published_at),%(decision_cutoff)s) AS latest_ts
              FROM corpus.document_search_index
            ),
            q AS (
              SELECT CASE WHEN %(numeric_token)s = ''
                     THEN websearch_to_tsquery('simple', %(query)s)
                     WHEN %(numeric_sign)s = '-'
                     THEN plainto_tsquery('simple', '-' || %(numeric_token)s)
                     WHEN %(numeric_sign)s = '+'
                     THEN plainto_tsquery('simple', %(numeric_token)s)
                     ELSE plainto_tsquery('simple', %(numeric_token)s)
                       || plainto_tsquery('simple', '-' || %(numeric_token)s) END AS tsq
            ),
            candidates AS MATERIALIZED (
              SELECT s.document_id,s.published_at
              FROM corpus.document_search_index s
              CROSS JOIN bounds b
              CROSS JOIN q
              WHERE s.published_at >= b.latest_ts - ((%(days)s::int - 1) * interval '1 day')
                AND s.published_at <= %(decision_cutoff)s
                AND (%(has_sources)s = false OR s.source_type = ANY(%(source_types)s))
                AND (%(stock_code)s = '' OR s.stock_codes @> ARRAY[%(stock_code)s]::text[])
                AND (
                  (%(exact_only)s AND (s.search_vector @@ q.tsq
                    OR (s.metadata->>'fts_vector_prefix_only'='true'
                        AND (
                          (%(numeric_token)s <> ''
                            AND s.search_text ILIKE ANY(%(numeric_patterns)s)
                            AND s.search_text ~ %(numeric_regex)s)
                          OR (%(numeric_token)s = ''
                            AND s.search_text ILIKE ALL(%(exact_patterns)s))
                        ))))
                  OR (NOT %(exact_only)s AND (
                  (
                    %(use_trigram)s = true
                    AND s.search_text ILIKE ANY(%(patterns)s)
                  )
                  OR (
                    %(use_trigram)s = false
                    AND (
                      s.search_vector @@ q.tsq
                      OR s.search_text ILIKE ANY(%(patterns)s)
                    )
                  )
                  ))
                )
              -- search_text already includes the title. A separate title OR
              -- plus a global date sort made broad CJK queries read/recheck
              -- every matching full body before LIMIT (over 100k matches).
              -- Bound broad index candidates first, then rank/hydrate only
              -- that pool below. Exact numeric/phrase lookup keeps date order.
              ORDER BY CASE WHEN %(exact_only)s THEN s.published_at END DESC NULLS LAST
              LIMIT %(candidate_limit)s
            ), matched AS (
              SELECT s.*,ts_rank_cd(s.search_vector,q.tsq) AS rank_score
              FROM candidates c JOIN corpus.document_search_index s USING(document_id)
              CROSS JOIN q
              ORDER BY rank_score DESC NULLS LAST,s.published_at DESC NULLS LAST
              LIMIT %(limit)s
            )
            SELECT document_id, source_type, source_name, published_at::date AS published_date,
                   title, url, keywords, stock_codes, industries, event_types, search_text,
                   round(rank_score::numeric, 6) AS rank_score,
                   metadata
            FROM matched
            """,
            {
                "query": query or " ".join(terms),
                "numeric_token": numeric_token,
                "numeric_sign": numeric_sign,
                "exact_only": exact_only,
                "decision_cutoff": decision_cutoff or dt.datetime.now(dt.timezone.utc),
                "patterns": patterns,
                "exact_patterns": exact_patterns,
                "numeric_patterns": numeric_patterns,
                "numeric_regex": numeric_regex,
                "use_trigram": use_trigram,
                "days": days,
                "has_sources": bool(source_types),
                "source_types": source_types,
                "stock_code": stock_code,
                "limit": limit,
                "candidate_limit": max(200,min(1000,limit*20)),
            },
        ).fetchall()
    try:
        # Large TOASTed full bodies make broad sequential substring scans far
        # more expensive than the planner estimates. Prefer the existing GIN
        # indexes for this bounded recall query only, preserving caller settings
        # and its transaction even when a statement times out.
        with conn.transaction():
            previous = conn.execute("SHOW enable_seqscan").fetchone()["enable_seqscan"]
            conn.execute("SET LOCAL enable_seqscan=off")
            rows = fetch_rows(True) if prefer_exact else []
            if not rows:
                rows = fetch_rows(False)
            conn.execute("SELECT set_config('enable_seqscan', %s, true)", (previous,))
    except Exception as exc:
        return [], {"fts_used": False, "fts_reason": f"{type(exc).__name__}: {exc}"[:240]}

    result: list[dict[str, Any]] = []
    for row in rows:
        metadata = row.get("metadata") or {}
        keywords = row.get("keywords") or []
        matched_keywords = [
            keyword
            for keyword in keywords
            if any(term.lower() in str(keyword).lower() for term in terms)
        ]
        common = {
            "source_type": row.get("source_type"),
            "source_name": row.get("source_name"),
            "published_date": row.get("published_date"),
            "title": row.get("title"),
            "url": row.get("url"),
            "stock_code": (row.get("stock_codes") or [None])[0],
            "stock_name": metadata.get("stock_name"),
            "institution": metadata.get("institution"),
            "rating": metadata.get("rating"),
            "rating_change": metadata.get("rating_change"),
            "industry_name": metadata.get("industry_name") or ((row.get("industries") or [None])[0]),
            "matched_keywords": matched_keywords,
            "snippet": first_snippet(row.get("search_text") or "", terms + numeric_variants, snippet_chars),
        }
        if bm25:
            result.append(
                {
                    "id": str(row.get("document_id")),
                    **common,
                    "keywords": keywords,
                    "candidate_score": row.get("rank_score"),
                    "bm25_score": row.get("rank_score"),
                    "matched_terms": [term for term in terms if term.lower() in (row.get("search_text") or "").lower()],
                }
            )
        else:
            result.append(
                {
                    "document_id": str(row.get("document_id")),
                    **common,
                    "score": row.get("rank_score"),
                }
            )
    return result, {"fts_used": True, "fts_hits": len(result),"fts_candidate_cap":max(200,min(1000,limit*20)),"fts_patterns":patterns,
                   'fts_candidate_policy':'bounded_full_text_candidates_then_rank',
                   'fts_recall_exhaustive':False,
                   'fts_full_text_trigram_documents':sum(bool((r.get('metadata') or {}).get('fts_vector_prefix_only')) for r in rows)}


def emit(rows, meta=None) -> None:
    print(json.dumps({"meta": meta or {}, "rows": rows}, ensure_ascii=False, default=str, indent=2))


def display_timestamps(value, timezone="Asia/Shanghai"):
    """Normalize aware instants only. Reporting periods and naive values stay intact."""
    zone=ZoneInfo(timezone)
    if isinstance(value, dt.datetime):
        return value.astimezone(zone).isoformat() if value.tzinfo is not None else value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})", value):
        try:
            return dt.datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(zone).isoformat()
        except ValueError:
            return value
    if isinstance(value, dict):
        return {k:display_timestamps(v,timezone) for k,v in value.items()}
    if isinstance(value, (list,tuple)):
        return [display_timestamps(v,timezone) for v in value]
    return value


def emit_v3(
    rows: list[dict[str, Any]],
    *,
    request_id: str,
    decision_cutoff: dt.datetime,
    evidence: list[dict[str, Any]] | None = None,
    status: str = "ok",
    data_through: Any = None,
    coverage_pct: float | None = None,
    quality_status: str = "unverified",
    warnings: list[str] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> None:
    meta = {
        "request_id": request_id,
        "tool_version": "finance-agent:v3.0",
        "display_timezone": "Asia/Shanghai",
        "timestamp_semantics": "Aware timestamps use display_timezone; report periods and trading dates are unchanged.",
        "decision_cutoff": decision_cutoff,
        "data_through": data_through,
        "status": status,
        "coverage_pct": coverage_pct,
        "quality_status": quality_status,
        "warnings": warnings or [],
        "row_count": len(rows),
        "read_only": True,
    }
    meta.update(extra_meta or {})
    print(json.dumps(
        display_timestamps({"meta": meta, "rows": rows, "evidence": evidence or []}, meta["display_timezone"]),
        ensure_ascii=False, default=str, indent=2,
    ))


def v3_request_context(spec: dict[str, Any]) -> tuple[str, dt.datetime]:
    request_id = str(spec.get("request_id") or uuid.uuid4())
    raw_cutoff = str(spec.get("decision_cutoff") or "").strip()
    if raw_cutoff:
        try:
            cutoff = dt.datetime.fromisoformat(raw_cutoff.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("decision_cutoff must be an ISO-8601 timestamp") from exc
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        cutoff = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    return request_id, cutoff.astimezone(ZoneInfo("Asia/Shanghai"))


def v3_evidence(
    request_id: str,
    index: int,
    *,
    claim_type: str,
    entity_id: Any = None,
    metric_id: Any = None,
    value: Any = None,
    unit: Any = None,
    period_end: Any = None,
    as_of_date: Any = None,
    available_at: Any = None,
    source_id: Any = None,
    source_document_id: Any = None,
    quality_status: str = "unverified",
) -> dict[str, Any]:
    return {
        "evidence_id": f"{request_id}:E{index:04d}",
        "claim_type": claim_type,
        "entity_id": str(entity_id) if entity_id is not None else None,
        "metric_id": str(metric_id) if metric_id is not None else None,
        "value": value,
        "unit": unit,
        "period_end": period_end,
        "as_of_date": as_of_date,
        "available_at": available_at,
        "source_id": str(source_id) if source_id is not None else None,
        "source_document_id": str(source_document_id) if source_document_id else None,
        "quality_status": quality_status,
    }


def run_v3_time_context(root: str, spec: dict[str, Any]) -> None:
    del root
    request_id, cutoff = v3_request_context(spec)
    timezone_name = str(spec.get("timezone") or "Asia/Shanghai")
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone_name = "Asia/Shanghai"
        timezone = ZoneInfo(timezone_name)
    now = dt.datetime.now(timezone)
    rows = [{
        "timezone": timezone_name,
        "current_time": now,
        "today": now.date(),
        "yesterday": now.date() - dt.timedelta(days=1),
        "weekday": now.strftime("%A"),
    }]
    evidence_value = {
        **rows[0],
        "hour": now.hour,
        "minute": now.minute,
        "second": now.second,
        "utc_offset_hours": now.utcoffset().total_seconds() / 3600,
    }
    evidence = [v3_evidence(
        request_id, 1, claim_type="data_quality", entity_id=timezone_name,
        metric_id="current_time", value=evidence_value, unit="local_time",
        as_of_date=now.date(), available_at=now, source_id="system:clock",
        quality_status="verified",
    )]
    emit_v3(
        rows, request_id=request_id, decision_cutoff=cutoff,
        evidence=evidence,
        status="ok", data_through=now, coverage_pct=100.0, quality_status="verified",
        extra_meta={"relative_dates_must_be_rendered_as_absolute_dates": True,"display_timezone":timezone_name},
    )


def run_v3_data_status(root: str, spec: dict[str, Any]) -> None:
    request_id, cutoff = v3_request_context(spec)
    dataset_keys = source_types_arg(spec.get("dataset_keys"))
    required_only = bool(spec.get("required_only", False))
    limit = clamp_int(spec.get("limit"), 50, 1, 100)
    scope = str(spec.get("scope") or "status")
    if scope not in {"status", "catalog"}:
        raise ValueError("scope must be status or catalog")
    if scope == "catalog":
        if str(root) not in sys.path: sys.path.insert(0, str(root))
        from scripts.analytics.data_catalog import load_data_catalog
        with connect(root) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            catalog_limit=min(limit,10)
            catalog_offset=int(spec.get('catalog_offset',0))
            rows = load_data_catalog(conn, cutoff, dataset_keys, catalog_limit, offset=catalog_offset)
        # A paginated result does not prove requested keys on later pages absent.
        missing = sorted(set(dataset_keys) - {row['dataset_key'] for row in rows}) if not catalog_offset and len(dataset_keys)<=catalog_limit else []
        evidence = [v3_evidence(request_id, i, claim_type='data_quality',
            entity_id=row['dataset_key'], metric_id='registered_data_contract', value=row,
            unit='dataset', available_at=row['catalog_available_at'],
            source_id='ops.data_asset_registry', quality_status='unverified')
            for i, row in enumerate(rows, 1)]
        emit_v3(rows, request_id=request_id, decision_cutoff=cutoff, evidence=evidence,
            status='invalid_request' if missing else 'ok' if rows else 'unavailable',
            quality_status='unknown', coverage_pct=None,
            extra_meta={'scope':'catalog','hard_gate_passed':None,'missing_requested':missing,
                'catalog_offset':catalog_offset,'catalog_page_size':catalog_limit,
                'catalog_next_offset':catalog_offset+len(rows) if len(rows)==catalog_limit else None,
                'registration_is_not_coverage':True,'catalog_kind':'current_operational_definitions',
                'instruction':'Use usage_contract and grain to select data. not_measured means no direct watermark, not absent data. Read scope=status for gates.'})
        return
    required = [
        "agent.daily_data_gate",
        "market.trading_calendar", "market.canonical_bars_daily_v2",
        "market.asset_lifecycle_versions", "market.corporate_actions",
        "market.adjustment_factors_daily",
        "company.filings", "company.financial_facts", "corpus.document_quality",
        "corpus.document_embeddings", "factor.daily_scores", "kg.temporal_graph",
    ]
    if required_only and not dataset_keys:
        dataset_keys = required
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            SELECT dataset_key,partition_key,expected_date,data_through,row_count,
                   eligible_count,coverage_pct,quality_status,schema_version,
                   source_version,run_id,completed_at,details
            FROM ops.dataset_watermarks
            WHERE completed_at <= %(cutoff)s
              AND (%(has_keys)s=false OR dataset_key=ANY(%(keys)s))
              AND (%(required_only)s=false OR partition_key='default')
            ORDER BY
              CASE WHEN dataset_key=ANY(%(required)s) THEN 0 ELSE 1 END,
              quality_status='fail' DESC,dataset_key,partition_key
            LIMIT %(limit)s
            """,
            {"cutoff": cutoff, "has_keys": bool(dataset_keys), "keys": dataset_keys,
             "required_only": required_only,
             "required": required, "limit": limit},
        ).fetchall()
    covered = {row["dataset_key"] for row in rows}
    missing_requested = [key for key in dataset_keys if key not in covered]
    missing = [key for key in required if key not in covered] if required_only else []
    failures = [row["dataset_key"] for row in rows if row["quality_status"] == "fail"]
    quality = "fail" if failures or missing else (
        "warn" if any(row["quality_status"] in {"warn", "degraded", "unknown"} for row in rows) else "pass"
    )
    evidence = [v3_evidence(
        request_id, index, claim_type="data_quality", entity_id=row["dataset_key"],
        metric_id="dataset_watermark", value={
            "data_through": row["data_through"], "row_count": row["row_count"],
            "coverage_pct": row["coverage_pct"], "quality_status": row["quality_status"],
        }, unit="dataset", as_of_date=row["expected_date"], available_at=row["completed_at"],
        source_id=row["source_version"], quality_status=(
            "verified" if row["quality_status"] == "pass" else
            "incomplete" if row["quality_status"] in {"warn", "degraded", "unknown"} else "rejected"
        ),
    ) for index, row in enumerate(rows, 1)]
    emit_v3(
        rows, request_id=request_id, decision_cutoff=cutoff, evidence=evidence,
        status="invalid_request" if missing_requested and not required_only else "blocked" if quality == "fail" else "ok", quality_status=quality,
        data_through=min((row["data_through"] for row in rows if row["data_through"]), default=None),
        coverage_pct=None,
        warnings=([f"Unmatched dataset_keys: {', '.join(missing_requested)}. Omit dataset_keys to list the catalog; this does not prove company data is missing."] if missing_requested else [])
                 + ([f"missing required watermarks: {', '.join(missing)}"] if missing else [])
                 + ([f"hard-gate failures: {', '.join(sorted(set(failures)))}"] if failures else []),
        extra_meta={"hard_gate_passed": None if missing_requested and not required_only else quality != "fail",
                    "missing_required": missing, "missing_requested": missing_requested, "required_dataset_keys": required,
                    "coverage_policy":"Per-dataset denominators differ; inspect each row. Missing coverage is unknown, not zero."},
    )


def _v3_codes(spec: dict[str, Any], limit: int = 50) -> list[str]:
    codes = source_types_arg(spec.get("stock_codes"))
    one = str(spec.get("stock_code") or "").strip()
    if one:
        codes.append(one)
    return list(dict.fromkeys(codes))[:limit]


def run_v3_market_series_batch(root: str, spec: dict[str, Any]) -> None:
    request_id, cutoff = v3_request_context(spec)
    codes = _v3_codes(spec)
    start_date = str(spec.get("start_date") or "").strip() or (cutoff.date() - dt.timedelta(days=90)).isoformat()
    end_date = str(spec.get("end_date") or "").strip() or cutoff.date().isoformat()
    basis = str(spec.get("price_basis") or "total_return").strip().lower()
    if basis not in {"raw_unadjusted", "total_return"}:
        raise ValueError("price_basis must be raw_unadjusted or total_return")
    limit = clamp_int(spec.get("limit"), 1000, 1, 5000)
    if not codes:
        emit_v3([], request_id=request_id, decision_cutoff=cutoff, status="invalid_request",
                coverage_pct=0, quality_status="fail", warnings=["stock_codes is required"])
        return
    relation = "market.canonical_total_return_daily_v2" if basis == "total_return" else "market.canonical_bars_daily_v2"
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            f"""
            SELECT a.asset_id::text,a.symbol AS stock_code,a.name AS stock_name,b.dt,
                   round(b.open::numeric,4) AS open,round(b.high::numeric,4) AS high,
                   round(b.low::numeric,4) AS low,round(b.close::numeric,4) AS close,
                   round(b.volume::numeric,2) AS volume,round(b.amount::numeric,2) AS amount,
                   b.source,b.adjustment,b.available_at,b.quality_status
            FROM {relation} b
            JOIN market.assets a ON a.asset_id=b.asset_id
            WHERE a.symbol=ANY(%(codes)s)
              AND b.dt BETWEEN %(start_date)s::date AND %(end_date)s::date
              AND b.available_at <= %(cutoff)s
              AND b.quality_status NOT IN ('rejected','conflict')
            ORDER BY b.dt,a.symbol
            LIMIT %(limit)s
            """,
            {"codes": codes, "start_date": start_date, "end_date": end_date,
             "cutoff": cutoff, "limit": limit},
        ).fetchall()
    covered = sorted({row["stock_code"] for row in rows})
    coverage = round(100.0 * len(covered) / len(codes), 2)
    quality = "pass" if coverage == 100 and all(row["quality_status"] == "verified" for row in rows) else (
        "warn" if rows else "fail"
    )
    evidence = [v3_evidence(
        request_id, index, claim_type="market", entity_id=row["asset_id"],
        metric_id=f"ohlcv_{basis}", value={
            "stock_code": row["stock_code"], "stock_name": row["stock_name"],
            **{key: row[key] for key in ("open","high","low","close","volume","amount")},
        },
        unit="CNY; volume/amount source units retained", as_of_date=row["dt"],
        available_at=row["available_at"], source_id=row["source"],
        quality_status="verified" if row["quality_status"] == "verified" else "unverified",
    ) for index, row in enumerate(rows, 1)]
    period_returns = []
    for code in codes:
        code_rows = [row for row in rows if row["stock_code"] == code and row["close"] is not None]
        if len(code_rows) < 2:
            continue
        first = code_rows[0]
        last = code_rows[-1]
        first_close = float(first["close"])
        if first_close == 0:
            continue
        return_pct = round((float(last["close"]) / first_close - 1.0) * 100.0, 6)
        summary = {
            "stock_code": code,
            "stock_name": last["stock_name"],
            "start_date": first["dt"], "end_date": last["dt"],
            "start_close": first["close"], "end_close": last["close"],
            "return_pct": return_pct, "price_basis": basis,
        }
        period_returns.append(summary)
        evidence.append(v3_evidence(
            request_id, len(evidence) + 1, claim_type="calculation",
            entity_id=last["asset_id"], metric_id=f"period_return_pct_{basis}",
            value=summary, unit="percentage_points", as_of_date=last["dt"],
            available_at=max(first["available_at"], last["available_at"]),
            source_id="deterministic:period_return:v1",
            quality_status=(
                "verified" if first["quality_status"] == last["quality_status"] == "verified"
                else "unverified"
            ),
        ))
    emit_v3(
        rows, request_id=request_id, decision_cutoff=cutoff, evidence=evidence,
        status="ok" if rows else "unavailable", data_through=max((row["dt"] for row in rows), default=None),
        coverage_pct=coverage, quality_status=quality,
        warnings=[f"missing canonical V2 series for: {', '.join(sorted(set(codes)-set(covered)))}"] if coverage < 100 else [],
        extra_meta={
            "price_basis": basis, "source_relation": relation,
            "requested_stock_codes": codes, "period_returns": period_returns,
            "period_return_formula": "(end_close / start_close - 1) * 100",
        },
    )


def run_v3_fundamentals_batch(root: str, spec: dict[str, Any]) -> None:
    request_id, cutoff = v3_request_context(spec)
    codes = _v3_codes(spec)
    kinds = source_types_arg(spec.get("fact_kinds")) or [str(spec.get("fact_kind") or "actual")]
    allowed_kinds = {"actual", "forecast", "consensus", "report_claim"}
    kinds = list(dict.fromkeys(kind for kind in kinds if kind in allowed_kinds))
    metric_codes = source_types_arg(spec.get("metric_codes"))
    periods = clamp_int(spec.get("periods"), 12, 1, 40)
    if not codes or not kinds:
        emit_v3([], request_id=request_id, decision_cutoff=cutoff, status="invalid_request",
                coverage_pct=0, quality_status="fail", warnings=["valid stock_codes and fact_kinds are required"])
        return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH ranked AS (
              SELECT f.*,a.symbol,a.name,
                     row_number() OVER (
                       PARTITION BY f.asset_id,f.metric_code,f.fact_kind,f.statement_scope,f.period_end
                       ORDER BY f.revision_no DESC,f.available_at DESC,f.financial_fact_id DESC
                     ) AS revision_rank,
                     dense_rank() OVER (PARTITION BY a.symbol ORDER BY f.period_end DESC) AS period_rank
              FROM company.financial_facts f
              JOIN market.assets a ON a.asset_id=f.asset_id
              WHERE a.symbol=ANY(%(codes)s) AND f.fact_kind=ANY(%(kinds)s)
                AND (%(has_metrics)s=false OR f.metric_code=ANY(%(metrics)s))
                AND f.available_at <= %(cutoff)s
                AND f.quality_status NOT IN ('rejected','conflict')
                AND coalesce(f.metadata->>'revalidation_required','false') <> 'true'
            )
            SELECT financial_fact_id,asset_id::text,symbol AS stock_code,name AS stock_name,
                   metric_code,metric_name,fact_kind,statement_scope,period_type,
                   period_start,period_end,value,yoy,
                   CASE WHEN yoy IS NOT NULL THEN 'percent' END AS yoy_unit,
                   unit,currency,announced_at,available_at,
                   revision_no,source_name,source_document_id,confidence,quality_status
            FROM ranked WHERE revision_rank=1 AND period_rank <= %(periods)s
            ORDER BY symbol,fact_kind,period_end DESC,metric_code
            LIMIT 2000
            """,
            {"codes": codes, "kinds": kinds, "has_metrics": bool(metric_codes),
             "metrics": metric_codes, "cutoff": cutoff, "periods": periods},
        ).fetchall()
    covered = sorted({row["stock_code"] for row in rows})
    coverage = round(100.0 * len(covered) / len(codes), 2)
    evidence = [v3_evidence(
        request_id, index, claim_type=row["fact_kind"], entity_id=row["asset_id"],
        metric_id=row["metric_code"], value={
            "stock_code": row["stock_code"], "stock_name": row["stock_name"],
            "value": row["value"], "yoy": row["yoy"], "yoy_unit": row["yoy_unit"],
        },
        unit=row["unit"], period_end=row["period_end"], available_at=row["available_at"],
        source_id=row["source_name"], source_document_id=row["source_document_id"],
        quality_status="verified" if row["quality_status"] == "verified" else "unverified",
    ) for index, row in enumerate(rows, 1)]
    emit_v3(
        rows, request_id=request_id, decision_cutoff=cutoff, evidence=evidence,
        status="ok" if rows else "unavailable", data_through=max((row["period_end"] for row in rows), default=None),
        coverage_pct=coverage, quality_status="pass" if coverage == 100 and all(r['quality_status']=='verified' for r in rows) else ("warn" if rows else "fail"),
        warnings=["fact kinds are isolated in each row; do not combine actual, forecast, consensus, and report_claim as one metric"]
                 + ([f"no financial facts for: {', '.join(sorted(set(codes)-set(covered)))}"] if coverage < 100 else []),
        extra_meta={"fact_kinds": kinds, "actual_forecast_mixing_forbidden": True},
    )


def frozen_research(conn, root, cutoff, codes=None, as_of=None, limit=10):
    if str(root) not in sys.path: sys.path.insert(0,str(root))
    from scripts.analytics.research_snapshot import load_snapshot,compact_research
    return compact_research(load_snapshot(conn,cutoff,codes,as_of,limit))


def run_v3_factor_analysis(root: str, spec: dict[str, Any]) -> None:
    if spec.get('mode') == 'validation':
        return run_v3_factor_validation(root,spec)
    if spec.get('mode', 'signals') != 'signals':
        raise ValueError('factor mode must be signals or validation')
    request_id, cutoff = v3_request_context(spec)
    codes = _v3_codes(spec)
    factor_names = source_types_arg(spec.get("factor_names"))
    as_of_date = str(spec.get("as_of_date") or cutoff.date().isoformat())
    include_candidate = bool(spec.get("include_candidate", False))
    lifecycles = ["shadow", "production", "degraded"] + (["candidate"] if include_candidate else [])
    limit = clamp_int(spec.get("limit"), 100, 1, 500)
    # Old cache rows cannot inherit a newer feature contract. Use the validated
    # score view and preserve the original system cutoff on score materialization.
    score_source = """(
        SELECT DISTINCT ON(s.factor_id,s.asset_id) s.*,d.lifecycle_status
        FROM factor.valid_daily_scores_v1 s
        JOIN market.assets a USING(asset_id) JOIN factor.definitions d USING(factor_id)
        WHERE s.dt BETWEEN %(as_of_date)s::date-60 AND %(as_of_date)s::date
          AND s.available_at<=%(cutoff)s AND s.updated_at<=%(cutoff)s
          AND (%(has_codes)s=false OR a.symbol=ANY(%(codes)s))
          AND (%(has_factors)s=false OR d.factor_name=ANY(%(factors)s))
          AND d.lifecycle_status=ANY(%(lifecycles)s)
        ORDER BY s.factor_id,s.asset_id,s.dt DESC,s.available_at DESC
    )"""
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        # The validity view hashes the full date cohort. Repeated nested-loop
        # evaluation per requested score can exhaust the tool budget; hash/merge
        # joins preserve the same cross-sectional and version checks.
        conn.execute("SET LOCAL enable_nestloop=off")
        rows = conn.execute(
            f"""
            SELECT l.factor_id::text,l.asset_id::text,a.symbol AS stock_code,a.name AS stock_name,
                   d.factor_name,d.display_name,d.family,d.direction,d.lifecycle_status,d.version,
                   l.dt,l.raw_value,l.percentile,l.zscore,l.available_at,l.source_feature_date
            FROM {score_source} l
            JOIN factor.definitions d ON d.factor_id=l.factor_id
            JOIN market.assets a ON a.asset_id=l.asset_id
            WHERE l.dt <= %(as_of_date)s::date AND l.available_at <= %(cutoff)s
              AND l.lifecycle_status=ANY(%(lifecycles)s)
              AND (%(has_codes)s=false OR a.symbol=ANY(%(codes)s))
              AND (%(has_factors)s=false OR d.factor_name=ANY(%(factors)s))
            ORDER BY l.percentile DESC NULLS LAST,d.factor_name,a.symbol
            LIMIT %(limit)s
            """,
            {"as_of_date": as_of_date, "cutoff": cutoff, "lifecycles": lifecycles,
             "has_codes": bool(codes), "codes": codes, "has_factors": bool(factor_names),
             "factors": factor_names, "limit": limit},
        ).fetchall()
        definition_count=conn.execute("""SELECT count(*) n FROM factor.definitions
            WHERE lifecycle_status=ANY(%s) AND (%s::text[]='{}'::text[] OR factor_name=ANY(%s))""",
            (lifecycles,factor_names,factor_names)).fetchone()['n']
        expected_count=(len(codes)*int(definition_count)) if codes else None
        coverage=100*len({(r['stock_code'],r['factor_name']) for r in rows})/expected_count if expected_count else None
        # The full research snapshot can consume the response budget and evict
        # the requested factor rows. Retrieve it explicitly with research_bundle.
        shared={}
    evidence = [v3_evidence(
        request_id, index, claim_type="model_signal", entity_id=row["asset_id"],
        metric_id=row["factor_name"], value={
            "stock_code": row["stock_code"], "stock_name": row["stock_name"],
            "factor_name": row["factor_name"], "display_name": row["display_name"],
            "raw_value": row["raw_value"], "percentile": row["percentile"], "zscore": row["zscore"],
            "lifecycle_status": row["lifecycle_status"], "factor_version": row["version"],
        }, unit="factor-specific", as_of_date=row["dt"], available_at=row["available_at"],
        source_id=f"factor:{row['factor_name']}:v{row['version']}",
        quality_status="verified" if row["lifecycle_status"] == "production" else "unverified",
    ) for index, row in enumerate(rows, 1)]
    emit_v3(
        rows, request_id=request_id, decision_cutoff=cutoff, evidence=evidence,
        status="ok" if rows else "unavailable", data_through=max((row["dt"] for row in rows), default=None),
        coverage_pct=min(100.0,coverage) if coverage is not None else None, quality_status="pass" if coverage == 100 else ("warn" if rows else "fail"),
        warnings=["candidate/shadow/degraded factors are research signals, not probabilities or production recommendations"]
          + ([] if rows else ["该范围尚无通过当前数据契约的因子分数；旧版、过期和缺输入的分数已隔离，可继续使用已核实行情与财务证据分析。"]),
        extra_meta={"score_input_contract":"pit-input-v1","availability_reason":None if rows else "no_eligible_score_for_cutoff",
          "expected_factor_stock_pairs":expected_count,"returned_factor_stock_pairs":len(rows),"shared_research":shared,"allowed_lifecycle_statuses": lifecycles, "production_count": sum(row["lifecycle_status"] == "production" for row in rows)},
    )


def run_v3_factor_validation(root: str, spec: dict[str, Any]) -> None:
    request_id,cutoff=v3_request_context(spec)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.analytics.factor_validation import VALIDATION_VERSION, independent_windows
    _, cutoff = v3_request_context(spec)
    cutoff = min(cutoff, dt.datetime.now(dt.timezone.utc))
    if spec.get("as_of_date"):
        requested_day = dt.date.fromisoformat(str(spec["as_of_date"])[:10])
        cutoff = min(cutoff, dt.datetime.combine(requested_day, dt.time.max, ZoneInfo("Asia/Shanghai")))
    decision_date = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
    names=source_types_arg(spec.get('factor_names'))
    limit=clamp_int(spec.get('limit'),20,1,60)
    with connect(root) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        rows=conn.execute("""WITH ranked AS (
          SELECT d.factor_id,d.factor_name,d.display_name,d.lifecycle_status,d.version,e.sample_name,
            e.observation_count,e.date_count,e.coverage,e.rank_ic_mean,e.rank_ic_ir,
            e.top_bottom_spread_mean,e.turnover_mean,e.q_value,e.passed_gates,e.metrics,
            e.created_at,r.test_start,r.test_end,r.horizon_days,r.status AS run_status,
            row_number() OVER(PARTITION BY d.factor_id,r.horizon_days ORDER BY e.created_at DESC) AS rn,
            p.decision AS approval_decision,p.effective_date AS approval_effective_date,
            p.factor_version AS approval_factor_version,p.expires_at AS approval_expires_at,
            p.decision AS latest_approval_decision,p.created_at AS approval_created_at,p.approved_by,p.effective_date,
            h.dt AS health_date,h.coverage AS latest_coverage,h.raw_stddev,h.zero_value_ratio,
            (SELECT max(dt) FROM market.canonical_bars_daily WHERE dt<=%(decision_date)s) AS latest_market_date
          FROM factor.definitions d JOIN factor.evaluations e USING(factor_id)
          JOIN factor.experiment_runs r USING(run_id)
          LEFT JOIN LATERAL (SELECT * FROM factor.production_approvals p
            WHERE p.factor_id=d.factor_id AND p.created_at<=%(cutoff)s
            ORDER BY p.created_at DESC LIMIT 1) p ON true
          LEFT JOIN LATERAL (SELECT * FROM factor.factor_health_daily h WHERE h.factor_id=d.factor_id
            AND h.dt<=%(decision_date)s AND h.latest_available_at<=%(cutoff)s AND h.computed_at<=%(cutoff)s
            ORDER BY h.dt DESC LIMIT 1) h ON true
          WHERE e.sample_name='test' AND e.created_at<=%(cutoff)s AND r.test_end<=%(decision_date)s
            AND r.horizon_days IN (5,20,60) AND r.finished_at<=%(cutoff)s
            AND coalesce(nullif(e.metrics->>'outcome_end','')::date,r.test_end)<=%(decision_date)s
            AND r.config->>'validation_state' IS DISTINCT FROM 'superseded'
            AND (%(names)s::text[]='{}' OR d.factor_name=ANY(%(names)s))
        ) SELECT * FROM ranked WHERE rn=1 ORDER BY factor_name,horizon_days LIMIT %(limit)s""",
          {'cutoff':cutoff,'names':names,'limit':limit,'decision_date':decision_date}).fetchall()
        evaluations = conn.execute("""SELECT e.*,r.test_start,r.test_end,r.horizon_days,r.status
            FROM factor.evaluations e JOIN factor.experiment_runs r USING(run_id)
            WHERE e.sample_name='test' AND e.factor_id=ANY(%(factor_ids)s)
              AND r.horizon_days IN (5,20,60)
              AND e.created_at<=%(cutoff)s AND r.finished_at<=%(cutoff)s
              AND r.test_end<=%(decision_date)s
              AND coalesce(nullif(e.metrics->>'outcome_end','')::date,r.test_end)<=%(decision_date)s
              AND r.config->>'validation_state' IS DISTINCT FROM 'superseded' """,
            {"factor_ids":[r["factor_id"] for r in rows],"cutoff":cutoff,"decision_date":decision_date}).fetchall() if rows else []
        forecast=conn.execute("""SELECT DISTINCT ON(horizon_days) model_version,horizon_days,
          trained_through,status,approved_at,validation,created_at FROM analytics.forecast_models
          WHERE created_at<=%s AND decision_cutoff<=%s AND horizon_days IN (5,20,60)
          ORDER BY horizon_days,created_at DESC""",(cutoff,cutoff)).fetchall()
    for row in rows:
        matching = [e for e in evaluations if e["factor_id"] == row["factor_id"]
                    and e["horizon_days"] == row.get("horizon_days")]
        row["passing_oos_windows"] = len(independent_windows(matching, row["version"]))
        row["current_validation_passed"] = bool(independent_windows([row], row["version"]))
        row["approval_valid_at_cutoff"] = bool(
            row.get("latest_approval_decision") == "approved"
            and str(row.get("approved_by") or "").strip()
            and row.get("approval_factor_version") == row["version"]
            and row.get("approval_created_at") is not None and row["approval_created_at"] <= cutoff
            and row.get("effective_date") is not None and row["effective_date"] <= decision_date
            and (row.get("approval_expires_at") is None or row["approval_expires_at"] > cutoff))
        checks = {
            "not_production": row.get("lifecycle_status") == "production",
            "fewer_than_3_independent_passing_oos_windows": row["passing_oos_windows"] >= 3,
            "latest_oos_current_contract_not_passed": row["current_validation_passed"],
            "unsupported_stock_horizon": row.get("horizon_days") in (5,20,60),
            "stale_or_missing_factor_health": row.get("health_date") is not None and row.get("health_date") == row.get("latest_market_date"),
            "coverage_below_80pct": float(row.get("latest_coverage") or 0) >= .80,
            "zero_cross_sectional_variance": row.get("raw_stddev") is not None and math.isfinite(float(row["raw_stddev"])) and float(row["raw_stddev"]) > 0,
            "factor_at_least_99pct_zero": row.get("zero_value_ratio") is not None and float(row["zero_value_ratio"]) < .99,
            "turnover_not_measured": row.get("turnover_mean") is not None,
            "nonpositive_top_bottom_spread": float(row.get("top_bottom_spread_mean") or 0) > 0,
            "missing_current_human_approval": row["approval_valid_at_cutoff"],
        }
        row["blockers"] = [reason for reason, passed in checks.items() if not passed]
        row["production_ready"] = not row["blockers"]
        row["prediction_eligible"] = row["production_ready"]
        row["required_validation_version"] = VALIDATION_VERSION
        row.pop('rn',None)
        row['production_approved'] = row['prediction_eligible']
    evidence=[v3_evidence(request_id,i,claim_type='model_validation',metric_id=r['factor_name'],
      value=r,unit='evaluation',as_of_date=r['test_end'],available_at=r['created_at'],
      source_id='factor.evaluations',quality_status='verified') for i,r in enumerate(rows,1)]
    emit_v3(rows,request_id=request_id,decision_cutoff=cutoff,evidence=evidence,
      status='ok' if rows else 'unavailable',quality_status='pass' if any(r['current_validation_passed'] for r in rows) else 'warn',
      data_through=max((r['test_end'] for r in rows),default=None),
      warnings=['Validation metrics describe historical out-of-sample tests, not guaranteed future returns. Lifecycle is the current registry state.'],
      extra_meta={'mode':'validation','forecast_model_validation':forecast,'production_approved_count':sum(r['production_approved'] for r in rows),
        'required_validation_version':VALIDATION_VERSION,'minimum_independent_oos_windows':3,'supported_horizons':[5,20,60]})


def bound_graph_pairs(rows, evidence, budget=18000):
    """Keep each returned relation and its citation together under the tool budget."""
    available=len(rows)
    while len(rows)>1 and len(json.dumps({"rows":rows,"evidence":evidence},ensure_ascii=False,default=str))>budget:
        rows.pop()
        evidence.pop(len(rows))
    result={"rows_available":available,"rows_omitted_from_output":available-len(rows),
            "returned_relations_are_sample":True}
    if len(rows)<available:result['output_compacted']=True
    return result


def run_v3_graph_analysis(root: str, spec: dict[str, Any]) -> None:
    request_id, cutoff = v3_request_context(spec)
    query = str(spec.get("query") or "").strip()
    codes = _v3_codes(spec)
    if not query and not codes:
        emit_v3([],request_id=request_id,decision_cutoff=cutoff,status='invalid_request',quality_status='fail',
          warnings=['Provide a company/concept query or stock_codes for a bounded graph expansion. Use finance_data_status for dataset health.'])
        return
    relation_types = source_types_arg(spec.get("relation_types"))
    as_of_date = str(spec.get("as_of_date") or cutoff.date().isoformat())
    limit = clamp_int(spec.get("limit"), 30, 1, 100)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH matched_entities AS MATERIALIZED (
              SELECT entity_id FROM kg.market_entities
              WHERE (%(query)s<>'' AND name ILIKE '%%'||%(query)s||'%%')
              UNION SELECT entity_id FROM kg.market_entities
              WHERE %(has_codes)s AND metadata->>'stock_code'=ANY(%(codes)s)
            ), matching_relations AS MATERIALIZED (
              SELECT r.* FROM kg.relations r JOIN matched_entities e ON e.entity_id=r.source_entity_id
              UNION SELECT r.* FROM kg.relations r JOIN matched_entities e ON e.entity_id=r.target_entity_id
              UNION SELECT r.* FROM kg.relations r
              WHERE (%(query)s='' AND %(has_codes)s=false)
            ), matching_versions AS MATERIALIZED (
              SELECT v.* FROM matching_relations r
              JOIN LATERAL (
                SELECT v.* FROM kg.relation_versions v
                WHERE v.relation_id=r.relation_id
                  AND v.system_known_during @> %(cutoff)s::timestamptz
                OFFSET 0
              ) v ON true
              UNION ALL
              SELECT frozen.* FROM matching_relations r
              JOIN kg.relation_versions identity ON identity.relation_id=r.relation_id
              JOIN kg.version_revision_history h ON h.source_version_id=identity.relation_version_id
                AND h.version_kind='relation'
                AND h.system_known_during @> %(cutoff)s::timestamptz
              CROSS JOIN LATERAL jsonb_populate_record(NULL::kg.relation_versions,h.frozen_record) frozen
            )
            SELECT rv.relation_version_id::text,r.relation_id::text,r.relation_type,
                   s.entity_id::text AS source_entity_id,s.entity_type AS source_entity_type,
                   s.name AS source_name,s.source_ref AS source_ref,
                   t.entity_id::text AS target_entity_id,t.entity_type AS target_entity_type,
                   t.name AS target_name,t.source_ref AS target_ref,
                   lower(rv.valid_during) AS valid_from,upper(rv.valid_during) AS valid_to,
                   lower(rv.public_known_during) AS public_known_from,
                   upper(rv.public_known_during) AS public_known_to,
                   rv.confidence,rv.weight,rv.direction,rv.numeric_value,rv.unit,rv.currency,
                   rv.directness,rv.review_status,rv.status,rv.source_system,
                   rv.extractor_version,rv.attributes,
                   ev.document_id AS source_document_id,ev.public_at AS evidence_public_at,
                   doc.title AS source_document_title,doc.source_type AS source_document_type,
                   doc.source_name AS source_publisher,
                   left(ev.evidence_text,500) AS evidence_text,
                   CASE
                     WHEN rv.review_status='approved' AND rv.source_system ~* '(cninfo|sse|szse|bse|official)'
                       THEN 'observed_fact'
                     WHEN rv.extractor_version ~* '(llm|model)' THEN 'model_inference'
                     ELSE 'extracted_claim'
                   END AS claim_class
            FROM matching_versions rv
            JOIN matching_relations r ON r.relation_id=rv.relation_id
            JOIN kg.market_entities s ON s.entity_id=r.source_entity_id
            JOIN kg.market_entities t ON t.entity_id=r.target_entity_id
            LEFT JOIN LATERAL (
              SELECT e.document_id,e.public_at,e.evidence_text
              FROM (
                SELECT e.* FROM kg.relation_version_evidence e
                WHERE e.relation_version_id=rv.relation_version_id
                UNION ALL
                SELECT frozen.* FROM kg.version_revision_history h
                CROSS JOIN LATERAL jsonb_populate_recordset(NULL::kg.relation_version_evidence,h.frozen_evidence) frozen
                WHERE h.version_kind='relation' AND h.source_version_id=rv.relation_version_id
                  AND h.system_known_during @> %(cutoff)s::timestamptz
              ) e
              WHERE e.relation_version_id=rv.relation_version_id AND e.system_at <= %(cutoff)s AND e.public_at <= %(cutoff)s
              ORDER BY e.public_at DESC NULLS LAST,e.created_at DESC LIMIT 1
            ) ev ON true
            LEFT JOIN corpus.documents doc ON doc.id=ev.document_id
            WHERE ev.document_id IS NOT NULL AND nullif(trim(ev.evidence_text),'') IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM corpus.document_quarantine q WHERE q.document_id=ev.document_id AND q.released_at IS NULL)
              AND rv.status='active' AND rv.review_status IN ('approved','classified')
              AND coalesce(rv.attributes->'attribution_check'->>'supported','true') <> 'false'
              AND coalesce(rv.attributes->>'superseded_by_canonical','false') <> 'true'
              AND rv.valid_during @> %(as_of_date)s::date
              AND rv.public_known_during @> %(cutoff)s::timestamptz
              AND rv.system_known_during @> %(cutoff)s::timestamptz
              AND (%(has_relations)s=false OR r.relation_type=ANY(%(relation_types)s))
            ORDER BY rv.review_status='approved' DESC,rv.confidence DESC,ev.public_at DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {"cutoff": cutoff, "as_of_date": as_of_date, "has_relations": bool(relation_types),
             "relation_types": relation_types, "query": query, "has_codes": bool(codes),
             "codes": codes, "limit": limit},
        ).fetchall()
        source_coverage=conn.execute("""SELECT sum(expected_documents) expected,
            sum(least(expected_documents,greatest(rules_processed,llm_processed))) processed
            FROM kg.source_coverage_daily WHERE dt BETWEEN %s::date-29 AND %s::date AND updated_at<=%s""",(as_of_date,as_of_date,cutoff)).fetchone()
        expected=int(source_coverage['expected'] or 0)
        coverage=100*float(source_coverage['processed'] or 0)/expected if expected else None
        # Focused graph queries should not attach unrelated watchlist forecasts.
        # Request shared_research explicitly through finance_research_bundle.
        shared={}
    paths=[]
    if (codes or query) and spec.get('include_paths',False):
        # Legacy path runner exposes a structured result, capture locally rather
        # than emitting a second JSON document to the tool caller.
        import contextlib,io
        captured=io.StringIO()
        with contextlib.redirect_stdout(captured):
            run_graph_paths(root,{'stock_code':codes[0] if codes else '', 'query':query,
                'as_of_date':as_of_date,'known_at':cutoff.isoformat(),'knowledge_mode':'system',
                'require_evidence':True,'review_statuses':['approved','classified'],'max_depth':2,'limit':5})
        paths=json.loads(captured.getvalue()).get('rows',[])
    # Internal review fingerprints belong in the audit table, not model context.
    for row in rows:
        row["attributes"] = {k:v for k,v in (row.get("attributes") or {}).items()
                             if k not in {"evidence_review", "review_contract"}}
    evidence = [v3_evidence(
        request_id, index, claim_type="graph_association", entity_id=row["source_entity_id"],
        metric_id=row["relation_type"], value={
            "source_name": row["source_name"], "source_ref": row["source_ref"],
            "target_name": row["target_name"], "target_ref": row["target_ref"],
            "target_entity_id": row["target_entity_id"], "confidence": row["confidence"],
            "weight": row["weight"], "direction": row["direction"], "claim_class": row["claim_class"],
            "review_status": row["review_status"], "world_time": [row["valid_from"], row["valid_to"]],
            "knowledge_time": [row["public_known_from"], row["public_known_to"]],
            "evidence_text": row["evidence_text"],
            "document_id": row["source_document_id"], "title": row["source_document_title"],
            "source_type": row["source_document_type"], "publisher": row["source_publisher"],
            "published_date": row["evidence_public_at"],
            "review_meaning": "classified checks source provenance; only approved official records can be observed facts",
            "interpretation_limit": (row.get("attributes") or {}).get("interpretation_limit"),
        }, unit=row["unit"], as_of_date=as_of_date, available_at=row["evidence_public_at"] or row["public_known_from"],
        source_id=row["source_system"], source_document_id=row["source_document_id"],
        quality_status="verified" if row["claim_class"] == "observed_fact" else "unverified",
    ) for index, row in enumerate(rows, 1)]
    for path in paths:
        evidence.append(v3_evidence(request_id,len(evidence)+1,claim_type='graph_association',
            entity_id=None,metric_id='temporal_graph_path',value=path,unit='association_not_causality',
            as_of_date=as_of_date,available_at=cutoff,source_id='kg.relation_versions',quality_status='unverified'))
    graph_output = bound_graph_pairs(rows, evidence)
    emit_v3(
        rows, request_id=request_id, decision_cutoff=cutoff, evidence=evidence,
        status="ok" if rows else "unavailable", data_through=max((row["evidence_public_at"] or row["public_known_from"] for row in rows), default=None),
        coverage_pct=None, quality_status="warn" if rows else "fail",
        warnings=["graph paths are associations, not causal proof; observed_fact, extracted_claim, and model_inference must remain distinct"],
        extra_meta={**graph_output,"coverage_basis":"graph completeness unknown; extraction coverage is a separate corpus-wide process metric","document_processing_coverage_pct":coverage,"expected_documents":expected,"paths":paths,"shared_research":shared,"world_time": as_of_date, "knowledge_time": cutoff, "causal_interpretation_forbidden": True,"review_policy":"approved official facts or source-classified claims; unreviewed, disputed, rejected, superseded and unsupported attribution excluded"},
    )



def research_analysis_status(rows, gate_quality):
    # A recommendation restriction must not discard valid factual evidence.
    return {
        'status': 'ok' if rows else 'unavailable',
        'quality_status': 'warn' if gate_quality == 'fail' and rows else gate_quality,
        'analysis_allowed': bool(rows),
        'recommendation_allowed': gate_quality == 'pass',
        'recommendation_status': 'eligible_for_claim_validation' if gate_quality == 'pass' else 'awaiting_data_validation',
    }


def run_v3_research_intelligence(root: str, spec: dict[str, Any]) -> None:
    if str(root) not in sys.path: sys.path.insert(0,str(root))
    from scripts.analytics.research_intelligence import load_intelligence, intelligence_rows
    request_id,cutoff=v3_request_context(spec)
    codes=_v3_codes(spec,limit=20)
    with connect(root) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        state=load_intelligence(conn,cutoff,spec.get('as_of_date'),codes,
            clamp_int(spec.get('limit'),10,1,20),str(spec.get('scope') or 'all'),spec.get('experiment_project'),spec.get('experiment_filters'),spec.get('experiment_offset',0),spec.get('method_query'))
    rows=intelligence_rows(state)
    evidence=[v3_evidence(request_id,i+1,claim_type='model_signal' if row['component']=='research_scenario' else 'interpretation',
        entity_id=row.get('stock_code') or row.get('entity_key'),metric_id=row['component'],value=row,
        as_of_date=row.get('as_of_date') or row.get('as_of_market_date'),available_at=row.get('available_at') or row.get('updated_at') or state['decision_cutoff'],
        source_id='versioned_research_intelligence',quality_status='unverified') for i,row in enumerate(rows)]
    emit_v3(rows,request_id=request_id,decision_cutoff=cutoff,evidence=evidence,
        status='ok' if rows else 'unavailable',quality_status='unverified',data_through=max((str(e['as_of_date']) for e in evidence if e.get('as_of_date')),default=None),
        extra_meta={'scope':state['scope'],'analysis_allowed':bool(rows),'recommendation_allowed':False,
            'usage':state['usage'],'focus_status':(state.get('focus') or {}).get('status'),
            'learning_status':(state.get('learning') or {}).get('update_status'),
            'candidate_pool_status':(state.get('candidate_pool') or {}).get('status'),
            'component_errors':state.get('component_errors',[]),
            'experiment_catalog':(state.get('experiments') or {}).get('catalog'),
            'experiment_offset':(state.get('experiments') or {}).get('offset'),
            'experiment_page_size':(state.get('experiments') or {}).get('page_size'),
            'experiment_next_offset':(state.get('experiments') or {}).get('next_offset')})


def run_v3_research_bundle(root: str, spec: dict[str, Any]) -> None:
    request_id, cutoff = v3_request_context(spec)
    codes = _v3_codes(spec, limit=20)
    stock_name = str(spec.get("stock_name") or "").strip()
    theme = str(spec.get("theme") or spec.get("query") or "").strip()
    subject = theme or stock_name or (codes[0] if codes else "")
    days = clamp_int(spec.get("days"), 180, 2, 1095)
    limit = clamp_int(spec.get("limit"), 8, 3, 20)
    supported={'market','fundamentals','factors','graph','documents','shared_research'}
    components=set(source_types_arg(spec.get('components'))) if 'components' in spec else supported
    if not components or not components <= supported:
        raise ValueError('components must be a nonempty subset of market, fundamentals, factors, graph, documents, shared_research')
    warnings: list[str] = []

    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        if not codes and stock_name:
            resolved = conn.execute(
                """
                SELECT symbol FROM market.assets
                WHERE asset_type='stock' AND name ILIKE '%%'||%s||'%%'
                ORDER BY (name=%s) DESC,updated_at DESC LIMIT 5
                """,
                (stock_name, stock_name),
            ).fetchall()
            codes = list(dict.fromkeys(row["symbol"] for row in resolved if row.get("symbol")))

        gate = conn.execute(
            """
            SELECT expected_date,data_through,quality_status,coverage_pct,details
            FROM ops.dataset_watermarks
            WHERE dataset_key='agent.daily_data_gate' AND partition_key='default'
              AND completed_at <= %s
            ORDER BY completed_at DESC LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
        market_rows = conn.execute(
            """
            SELECT DISTINCT ON (a.symbol) a.asset_id::text,a.symbol AS stock_code,
                   a.name AS stock_name,b.dt,b.close,b.volume,b.amount,b.source,
                   b.available_at,b.quality_status,b.price_basis
            FROM market.canonical_bars_daily_v2 b
            JOIN market.assets a ON a.asset_id=b.asset_id
            WHERE a.symbol=ANY(%s) AND b.available_at <= %s
              AND b.quality_status NOT IN ('rejected','conflict')
            ORDER BY a.symbol,b.dt DESC
            """,
            (codes, cutoff),
        ).fetchall() if codes and 'market' in components else []
        fundamental_rows = conn.execute(
            """
            WITH ranked AS (
              SELECT f.*,a.symbol,a.name,
                     row_number() OVER (
                       PARTITION BY f.asset_id,f.metric_code,f.period_end
                       ORDER BY f.revision_no DESC,f.available_at DESC
                     ) AS revision_rank,
                     dense_rank() OVER (PARTITION BY f.asset_id ORDER BY f.period_end DESC) AS period_rank
              FROM company.financial_facts f
              JOIN market.assets a ON a.asset_id=f.asset_id
              WHERE a.symbol=ANY(%s) AND f.fact_kind='actual'
                AND f.available_at <= %s AND f.quality_status NOT IN ('rejected','conflict')
                AND coalesce(f.metadata->>'revalidation_required','false') <> 'true'
            )
            SELECT financial_fact_id,asset_id::text,symbol AS stock_code,name AS stock_name,
                   metric_code,metric_name,period_end,value,yoy,unit,available_at,
                   source_name,source_document_id,quality_status
            FROM ranked WHERE revision_rank=1 AND period_rank<=4
            ORDER BY symbol,period_end DESC,
                     CASE metric_code WHEN 'revenue' THEN 0 WHEN 'net_profit_parent' THEN 1
                       WHEN 'operating_cash_flow' THEN 2 WHEN 'total_assets' THEN 3
                       WHEN 'total_liabilities' THEN 4 WHEN 'eps_basic' THEN 5 ELSE 10 END,
                     metric_code LIMIT %s
            """,
            (codes, cutoff,limit+1),
        ).fetchall() if codes and 'fundamentals' in components else []
        factor_rows = conn.execute(
            """
            SELECT l.asset_id::text,a.symbol AS stock_code,a.name AS stock_name,
                   d.factor_name,d.display_name,d.family,d.lifecycle_status,d.version,
                   l.dt,l.raw_value,l.percentile,l.zscore,l.available_at
            FROM (SELECT DISTINCT ON(s.asset_id,s.factor_id) s.*,d.lifecycle_status
                FROM factor.valid_daily_scores_v1 s JOIN factor.definitions d USING(factor_id)
                WHERE s.available_at<=%(history_cutoff)s AND s.updated_at<=%(history_cutoff)s
                  AND s.dt BETWEEN %(history_date)s::date-60 AND %(history_date)s
                  AND s.asset_id IN (SELECT asset_id FROM market.assets WHERE symbol=ANY(%(codes)s))
                ORDER BY s.asset_id,s.factor_id,s.dt DESC,s.available_at DESC) l
            JOIN factor.definitions d ON d.factor_id=l.factor_id
            JOIN market.assets a ON a.asset_id=l.asset_id
            WHERE a.symbol=ANY(%(codes)s) AND l.available_at<=%(history_cutoff)s
              AND l.lifecycle_status IN ('shadow','production','degraded')
            ORDER BY a.symbol,l.percentile DESC NULLS LAST LIMIT %(section_limit)s
            """,
            {"codes":codes,"history_cutoff":cutoff,"history_date":cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date(),"section_limit":limit+1},
        ).fetchall() if codes and 'factors' in components else []
        graph_rows = conn.execute(
            """
            SELECT g.relation_version_id::text,se.name AS source_name,se.metadata->>'stock_code' AS source_code,
                   te.name AS target_name,te.metadata->>'stock_code' AS target_code,g.relation_type,
                   g.valid_from,g.valid_to,g.public_known_from,g.confidence,g.review_status,
                   g.claim_class,g.source_system,ev.document_id::text AS source_document_id,
                   left(ev.evidence_text,650) AS evidence_text,
                   rv.attributes->>'interpretation_limit' AS interpretation_limit
            FROM (SELECT rv.relation_version_id,r.source_entity_id,r.target_entity_id,r.relation_type,
                lower(rv.valid_during) valid_from,upper(rv.valid_during) valid_to,
                lower(rv.public_known_during) public_known_from,rv.confidence,rv.review_status,rv.source_system,
                NULL::uuid source_document_id,
                CASE WHEN rv.review_status='approved' AND rv.source_system ~* '(official|cninfo|sse|szse)' THEN 'observed_fact'
                    WHEN rv.extractor_version ~* '(llm|model)' THEN 'model_inference' ELSE 'extracted_claim' END claim_class
                FROM kg.relation_versions rv JOIN kg.relations r USING(relation_id)) g
            JOIN kg.relation_versions rv ON rv.relation_version_id=g.relation_version_id
            JOIN kg.market_entities se ON se.entity_id=g.source_entity_id
            JOIN kg.market_entities te ON te.entity_id=g.target_entity_id
            LEFT JOIN LATERAL (
              SELECT e.document_id,e.evidence_text FROM kg.relation_version_evidence e
              WHERE e.relation_version_id=g.relation_version_id AND e.system_at<=%s AND e.public_at<=%s
              ORDER BY e.public_at DESC NULLS LAST,e.created_at DESC LIMIT 1
            ) ev ON true
            WHERE g.public_known_from<=%s AND rv.public_known_during @> %s::timestamptz
              AND rv.system_known_during @> %s::timestamptz AND rv.valid_during @> %s::date
              AND rv.status='active' AND rv.review_status IN ('approved','classified')
              AND coalesce(rv.attributes->>'superseded_by_canonical','false') <> 'true'
              AND coalesce(rv.attributes->'attribution_check'->>'supported','true') <> 'false' AND (
              %s<>'' AND (se.name ILIKE '%%'||%s||'%%' OR te.name ILIKE '%%'||%s||'%%')
              OR se.metadata->>'stock_code'=ANY(%s) OR te.metadata->>'stock_code'=ANY(%s)
            )
            ORDER BY (g.claim_class='observed_fact') DESC,g.confidence DESC NULLS LAST
            LIMIT %s
            """,
            (cutoff,cutoff,cutoff,cutoff,cutoff,cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date(),subject,subject,subject,codes,codes,limit+1),
        ).fetchall() if (subject or codes) and 'graph' in components else []

        shared=frozen_research(conn,root,cutoff,codes,cutoff.date(),limit+1) if 'shared_research' in components else {}

    terms = query_terms(subject or " ".join(codes))
    document_rows: list[dict[str, Any]] = []
    retrieval_meta: dict[str, Any] = {}
    if terms and 'documents' in components:
        document_rows, retrieval_meta = hybrid_document_rows(
            root, subject or " ".join(codes), terms,
            ["news", "announcement", "research_report", "peoples_daily"],
            days, codes[0] if len(codes) == 1 else "", limit, 520,
            decision_cutoff=cutoff,
        )

    rows: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    component_counts={}
    more_available=[]

    def add(component: str, source_rows: list[dict[str, Any]], claim_type: str) -> None:
        component_counts[component]=min(limit,len(source_rows))
        if len(source_rows)>limit: more_available.append(component)
        for source_row in source_rows[:limit]:
            row = {"component": component, **dict(source_row)}
            rows.append(row)
            evidence.append(v3_evidence(
                request_id, len(evidence) + 1, claim_type=claim_type,
                entity_id=row.get("asset_id") or row.get("stock_code") or row.get("source_code"),
                metric_id=row.get("metric_code") or row.get("factor_name") or row.get("relation_type") or component,
                value=row, unit=row.get("unit"), period_end=row.get("period_end"),
                as_of_date=row.get("dt") or row.get("as_of_date") or row.get("published_date") or row.get("valid_from"),
                available_at=row.get("available_at") or row.get("public_known_from") or row.get("published_date"),
                source_id=row.get("source") or row.get("source_name") or row.get("source_system"),
                source_document_id=row.get("source_document_id") or row.get("id"),
                quality_status="verified" if row.get("quality_status") == "verified"
                or row.get("claim_class") == "observed_fact" else "unverified",
            ))

    add("market", list(market_rows), "market")
    add("actual_fundamental", list(fundamental_rows), "actual")
    add("factor", list(factor_rows), "model_signal")
    add("graph", list(graph_rows), "graph_association")
    add("document", document_rows, "document_claim")
    add("shared_research",shared.get('rows',[]),"model_signal")
    if not codes: add("watchlist",shared.get('watchlist',[]),"model_signal")
    gate_quality = str(gate.get("quality_status") if gate else "fail")
    if gate_quality == "fail":
        warnings.append("daily data hard gate failed; this bundle cannot support stock recommendations")
    warnings.extend([
        "actual, forecast, consensus, and report claims are never merged; this bundle includes actual facts only",
        "factor rows are signals rather than probabilities; graph rows are associations rather than causality",
    ])
    active_channels = retrieval_meta.get("retrieval_channels") or []
    if 'documents' in components and len(active_channels) < 3:
        warnings.append("document retrieval did not return evidence from all three independent channels")
    data_dates = [row.get("dt") or row.get("period_end") or row.get("published_date") for row in rows]
    analysis_state = research_analysis_status(rows, gate_quality)
    emit_v3(
        rows, request_id=request_id, decision_cutoff=cutoff,evidence=evidence,
        status=analysis_state["status"],
        data_through=max((str(value) for value in data_dates if value), default=None),
        coverage_pct=gate.get("coverage_pct") if gate else 0.0,
        quality_status=analysis_state["quality_status"],warnings=warnings,
        extra_meta={
            "subject": subject, "resolved_stock_codes": codes,
            "deterministic_batch_workflow": True, **analysis_state, "recommendation_gate_quality":gate_quality,
            "component_counts": component_counts,"selected_components":sorted(components),
            "component_row_limit":limit,"more_available":more_available,
            "follow_up":"Use focused tools for additional periods, factor validation, graph evidence or original document text.",
            "retrieval": retrieval_meta,"shared_research":{k:v for k,v in shared.items() if k not in {'rows','watchlist'}},
            "price_basis_note":"market.close is raw traded close; total-return series is used only for adjusted returns and labels",
        },
    )


def document_excerpt(body: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Bounded, lossless line continuation; never skip a long truncated line."""
    pages=body.split('\f')
    if len(pages)>1 and not pages[-1].strip(): pages.pop()
    page=spec.get('page_number')
    if page is not None:
        page=int(page)
        if page<1 or page>len(pages): raise ValueError('page_number out of range')
        body=pages[page-1]
    lines=body.splitlines()
    start=clamp_int(spec.get('start_line'),1,1,max(1,len(lines)))
    needle=str(spec.get('needle') or '')
    found=None
    if needle:
        found=next((i+1 for i,line in enumerate(lines) if needle.casefold() in line.casefold()),None)
        if found is not None: start=max(1,found-4)
    count=clamp_int(spec.get('max_lines'),60,1,160)
    # Keep each selected line in full up to the character budget. A single
    # unusually long line is returned in bounded segments via char_offset.
    offset=clamp_int(spec.get('char_offset'),0,0,10000000)
    selected=[];used=0;next_offset=0;end=start-1
    for number in range(start,min(len(lines),start+count-1)+1):
        line=lines[number-1][offset if number==start else 0:]
        remaining=12000-used
        if len(line)+1>remaining:
            if not selected:
                selected.append(f'{number}: '+line[:remaining-1]);end=number
                next_offset=offset+remaining-1
            break
        selected.append(f'{number}: '+line);used+=len(line)+1;end=number
    next_line=end if next_offset else end+1 if end<len(lines) else None
    return {'text':'\n'.join(selected),'page_number':page,'page_count':len(pages),
      'page_boundaries_available':len(pages)>1,'start_line':start,'end_line':end,'total_lines':len(lines),
      'needle_found':found is not None if needle else None,'match_line':found,
      'next_line':next_line,'next_char_offset':next_offset,'truncated':next_line is not None}


def run_v3_document_read(root: str, spec: dict[str, Any]) -> None:
    request_id,cutoff=v3_request_context(spec)
    document_id=str(uuid.UUID(str(spec['document_id'])))
    with connect(root) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        row=conn.execute("""SELECT d.id::text AS document_id,d.title,d.source_type,d.source_name,d.url,
          d.published_at,d.metadata->>'stock_code' AS stock_code,
          coalesce(nullif(d.content_text,''),nullif(d.content_md,''),d.raw_text,'') AS body,
          q.quality_status AS body_quality
          FROM corpus.documents d LEFT JOIN corpus.document_quality q ON q.document_id=d.id
          WHERE d.id=%s AND d.published_at<=%s
            AND NOT EXISTS(SELECT 1 FROM corpus.document_quarantine x WHERE x.document_id=d.id AND x.released_at IS NULL)""",
          (document_id,cutoff)).fetchone()
    if not row or not row['body'].strip():
        emit_v3([],request_id=request_id,decision_cutoff=cutoff,status='unavailable',quality_status='fail',
          warnings=['Document is unavailable at this cutoff, quarantined, or its body has not been ingested.'],extra_meta={'document_id':document_id,'mode':'read'})
        return
    body=row.pop('body');row.update(document_excerpt(body,spec))
    evidence=[v3_evidence(request_id,1,claim_type='document_claim',entity_id=row['stock_code'],
      metric_id='original_document_excerpt',value={'title':row['title'],'text':row['text'],'page_number':row['page_number'],
      'start_line':row['start_line'],'end_line':row['end_line']},unit='document',available_at=row['published_at'],
      source_id=row['source_name'],source_document_id=document_id,quality_status='unverified')]
    emit_v3([row],request_id=request_id,decision_cutoff=cutoff,evidence=evidence,
      data_through=row['published_at'],quality_status='pass',extra_meta={'mode':'read','body_chars':len(body),
      'reading_cursor':{k:row[k] for k in ('page_number','page_count','start_line','end_line','total_lines','next_line','next_char_offset','needle_found','truncated')},
      'interpretation':'Verbatim source text. Financial units, table columns and issuer attribution still require checking.'})


def run_v3_document_retrieve(root: str, spec: dict[str, Any]) -> None:
    if spec.get('document_id'):
        return run_v3_document_read(root,spec)
    request_id, cutoff = v3_request_context(spec)
    query = str(spec.get("query") or "").strip()
    original_terms = bm25_query_terms(query, spec.get("keywords"))
    terms, expansion_matches = expand_terms_from_catalog(root, original_terms)
    source_types = source_types_arg(spec.get("source_types")) or [
        "news", "announcement", "peoples_daily", "research_report"
    ]
    days = clamp_int(spec.get("days"), 30, 1, 3650)
    stock_code = str(spec.get("stock_code") or "").strip()
    limit = clamp_int(spec.get("limit"), 20, 1, 50)
    snippet_chars = clamp_int(spec.get("snippet_chars"), 520, 120, 1200)
    if not terms:
        emit_v3(
            [], request_id=request_id, decision_cutoff=cutoff,status="invalid_request",
            coverage_pct=0.0,quality_status="fail",warnings=["query or keywords is required"],
        )
        return
    rows, retrieval_meta = hybrid_document_rows(
        root, query, terms, source_types, days, stock_code, limit, snippet_chars,
        decision_cutoff=cutoff,
    )
    channels = retrieval_meta.get("retrieval_channels") or []
    warnings = []
    if len(channels) < 3:
        warnings.append("one or more independent retrieval channels returned no candidates or failed")
    semantic_coverage=retrieval_meta.get('semantic_coverage_pct')
    if semantic_coverage is not None and semantic_coverage<95:
        warnings.append('Semantic backfill is incomplete. Returned lexical documents remain usable; an empty semantic result does not establish absence of evidence.')
    evidence = [v3_evidence(
        request_id,index,claim_type="document_claim",
        entity_id=row.get("stock_code"),metric_id="retrieved_document",
        value={
            "title": row.get("title"), "snippet": row.get("snippet"),
            "stock_code": row.get("stock_code"),
            "retrieval_channels": row.get("retrieval_channels"),
            "rrf_score": row.get("rrf_score"),
        },unit="document",as_of_date=row.get("published_date"),
        available_at=row.get("published_date"),source_id=row.get("source_name"),
        source_document_id=row.get("id") or row.get("document_id"),
        quality_status="unverified",
    ) for index,row in enumerate(rows,1)]
    emit_v3(
        rows,request_id=request_id,decision_cutoff=cutoff,evidence=evidence,
        status="ok" if rows else "unavailable",
        data_through=max(
            (str(row.get("published_date")) for row in rows if row.get("published_date")),
            default=None,
        ),
        coverage_pct=retrieval_meta.get("semantic_coverage_pct"),
        quality_status="pass" if len(channels) == 3 and semantic_coverage is not None and semantic_coverage>=95 else ("warn" if rows else "fail"),
        warnings=warnings,extra_meta={
            **retrieval_meta,"original_terms": original_terms,
            "expanded_terms": terms,"catalog_expansion": expansion_matches[:8],
            "point_in_time_upper_bound": cutoff,"independent_recall_required": True,
        },
    )


def run_v3_claim_verify(root: str, spec: dict[str, Any]) -> None:
    request_id, cutoff = v3_request_context(spec)
    turn_id = str(spec.get("turn_id") or "").strip()
    evidence_ids = source_types_arg(spec.get("evidence_ids"))
    if not turn_id or not evidence_ids:
        emit_v3([], request_id=request_id, decision_cutoff=cutoff, status="invalid_request",
                coverage_pct=0, quality_status="fail", warnings=["turn_id and evidence_ids are required"])
        return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            SELECT turn_id::text,evidence_id,tool_name,claim_type,entity_id,metric_id,
                   value_json AS value,unit,period_end,as_of_date,available_at,source_id,
                   source_document_id,quality_status,evidence_json,created_at
            FROM agent.turn_evidence
            WHERE turn_id=%(turn_id)s::uuid AND evidence_id=ANY(%(evidence_ids)s)
            ORDER BY evidence_id
            """,
            {"turn_id": turn_id, "evidence_ids": evidence_ids},
        ).fetchall()
    found = {row["evidence_id"] for row in rows}
    missing = sorted(set(evidence_ids) - found)
    emit_v3(
        rows, request_id=request_id, decision_cutoff=cutoff, evidence=rows,
        status="verified" if not missing else "incomplete", data_through=max((row["created_at"] for row in rows), default=None),
        coverage_pct=round(100.0 * len(found) / len(evidence_ids), 2),
        quality_status="pass" if not missing else "fail",
        warnings=[f"unknown evidence IDs: {', '.join(missing)}"] if missing else [],
        extra_meta={"verified_evidence_ids": sorted(found), "missing_evidence_ids": missing},
    )


def run_query(root: str, sql: str, limit: int) -> None:
    safe_limit = max(1, min(int(limit), 1000))
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(f"SELECT * FROM ({sql}) q LIMIT {safe_limit}").fetchall()
    emit(rows, {"limit": safe_limit, "row_count": len(rows), "read_only": True})


def run_schema(root: str, schema: str | None, table: str | None) -> None:
    schema_filter = schema if schema in ALLOWED_SCHEMAS else None
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        if table:
            table_name = table.split(".")[-1]
            table_schema = schema_filter
            if "." in table:
                left, right = table.split(".", 1)
                if left in ALLOWED_SCHEMAS:
                    table_schema = left
                    table_name = right
            rows = conn.execute(
                """
                SELECT table_schema, table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = ANY(%s)
                  AND table_name = %s
                  AND (%s::text IS NULL OR table_schema = %s)
                ORDER BY table_schema, table_name, ordinal_position
                """,
                (list(ALLOWED_SCHEMAS), table_name, table_schema, table_schema),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT table_schema, table_name
                FROM information_schema.tables
                WHERE table_schema = ANY(%s)
                  AND (%s::text IS NULL OR table_schema = %s)
                ORDER BY table_schema, table_name
                """,
                (list(ALLOWED_SCHEMAS), schema_filter, schema_filter),
            ).fetchall()
    emit(rows, {"row_count": len(rows), "read_only": True})


def run_theme_map(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("theme") or spec.get("query") or "").strip()
    limit = clamp_int(spec.get("limit"), 30, 1, 100)
    terms = theme_terms(query)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH latest AS (
              SELECT max(as_of_date) AS dt FROM analytics.ai_hardware_theme_scores
            ),
            matched_concepts AS (
              SELECT c.concept_id, c.name
              FROM kg.concepts c
              WHERE %(has_terms)s = false
                 OR c.name ILIKE ANY(%(patterns)s)
            ),
            theme AS (
              SELECT c.name AS theme,
                     count(DISTINCT s.stock_code) AS stock_count,
                     sum(s.mention_count_30d)::int AS mentions_30d,
                     sum(s.evidence_count_90d)::int AS evidence_90d,
                     round(avg(s.forward_pe)::numeric, 2) AS avg_forward_pe,
                     round(avg(s.forecast_eps_growth)::numeric, 4) AS avg_growth,
                     round(max(s.score)::numeric, 2) AS max_score,
                     (array_agg(DISTINCT s.stock_name))[1:12] AS sample_stocks
              FROM matched_concepts c
              JOIN analytics.ai_hardware_theme_scores s ON s.concept_id = c.concept_id
              JOIN latest l ON s.as_of_date = l.dt
              GROUP BY c.name
            )
            SELECT * FROM theme
            ORDER BY mentions_30d DESC NULLS LAST, evidence_90d DESC NULLS LAST, max_score DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "has_terms": bool(terms),
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
                "limit": limit,
            },
        ).fetchall()
    emit(rows, {"query": query, "terms": terms, "row_count": len(rows), "read_only": True})


def run_price_movers(root: str, spec: dict[str, Any]) -> None:
    days = clamp_int(spec.get("days"), 5, 1, 30)
    direction = str(spec.get("direction") or "down").lower()
    threshold = normalize_return_threshold(spec.get("threshold"), 0.2)
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    theme = str(spec.get("theme") or "").strip()
    terms = theme_terms(theme)
    ret_col = "pct_chg_1d" if days <= 1 else ("ret_5d" if days <= 7 else "ret_20d")
    op = "<=" if direction in ("down", "跌", "decline", "fall", "negative") else ">="
    cutoff = -threshold if op == "<=" else threshold
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            f"""
            WITH latest AS (
              SELECT max(dt) AS dt FROM analytics.asset_technical_daily
            ),
            latest_theme AS (
              SELECT max(as_of_date) AS dt FROM analytics.ai_hardware_theme_scores
            ),
            theme_pool AS (
              SELECT DISTINCT s.stock_code
              FROM analytics.ai_hardware_theme_scores s
              JOIN kg.concepts c ON c.concept_id = s.concept_id
              JOIN latest_theme lt ON s.as_of_date = lt.dt
              WHERE %(has_terms)s = false
                 OR c.name ILIKE ANY(%(patterns)s)
            )
            SELECT a.symbol AS stock_code, a.name AS stock_name, t.dt,
                   round(t.close::numeric, 3) AS close,
                   round(t.pct_chg_1d::numeric, 4) AS pct_chg_1d,
                   round(t.ret_5d::numeric, 4) AS ret_5d,
                   round(t.ret_20d::numeric, 4) AS ret_20d,
                   t.pattern_tags
            FROM analytics.asset_technical_daily t
            JOIN latest l ON t.dt = l.dt
            JOIN market.assets a ON a.asset_id = t.asset_id
            WHERE a.asset_type = 'stock'
              AND t.{ret_col} {op} %(cutoff)s
              AND (%(has_terms)s = false OR a.symbol IN (SELECT stock_code FROM theme_pool))
            ORDER BY t.{ret_col} {'ASC' if op == '<=' else 'DESC'} NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "cutoff": cutoff,
                "limit": limit,
                "has_terms": bool(terms),
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
            },
        ).fetchall()
    emit(rows, {"days": days, "return_column": ret_col, "threshold": threshold, "direction": direction, "theme": theme, "terms": terms, "row_count": len(rows), "read_only": True})


def run_asset_snapshot(root: str, spec: dict[str, Any]) -> None:
    as_of_date = str(spec.get("as_of_date") or "").strip()
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH selected_date AS (
              SELECT COALESCE(%(as_of_date)s::date, max(as_of_date)) AS dt
              FROM analytics.asset_snapshot_daily
            )
            SELECT
              s.stock_code, s.stock_name, s.as_of_date,
              s.technical_date, s.research_date, s.forecast_date, s.signal_date,
              round(s.close::numeric, 3) AS close,
              round(s.pct_chg_1d::numeric, 4) AS pct_chg_1d,
              round(s.ret_5d::numeric, 4) AS ret_5d,
              round(s.ret_20d::numeric, 4) AS ret_20d,
              round(s.pe_ttm::numeric, 2) AS pe_ttm,
              round(s.pb::numeric, 2) AS pb,
              round(s.forward_pe::numeric, 2) AS forward_pe,
              round(s.forecast_eps_growth::numeric, 4) AS forecast_eps_growth,
              s.forecast_institution_count_180d,
              s.latest_rating, s.latest_rating_change,
              s.recent_report_count, s.positive_rating_count, s.upgrade_count,
              s.actual_report_date, s.actual_report_period,
              round(s.actual_revenue_yoy::numeric, 4) AS actual_revenue_yoy,
              round(s.actual_net_profit_yoy::numeric, 4) AS actual_net_profit_yoy,
              round(s.actual_deducted_net_profit_yoy::numeric, 4) AS actual_deducted_net_profit_yoy,
              round(s.actual_financial_confidence::numeric, 3) AS actual_financial_confidence,
              round(s.composite_score::numeric, 3) AS composite_score,
              s.pattern_tags, s.source_dates
            FROM analytics.asset_snapshot_daily s
            JOIN selected_date d ON s.as_of_date = d.dt
            WHERE (%(stock_code)s = '' OR s.stock_code = %(stock_code)s)
              AND (%(stock_name)s = '' OR s.stock_name ILIKE '%%' || %(stock_name)s || '%%')
            ORDER BY s.composite_score DESC NULLS LAST,
                     s.positive_rating_count DESC NULLS LAST,
                     s.stock_code
            LIMIT %(limit)s
            """,
            {
                "as_of_date": as_of_date or None,
                "stock_code": stock_code,
                "stock_name": stock_name,
                "limit": limit,
            },
        ).fetchall()
    emit(
        rows,
        {
            "as_of_date": as_of_date or "latest",
            "stock_code": stock_code,
            "stock_name": stock_name,
            "row_count": len(rows),
            "read_only": True,
            "instruction": "Use source_dates to disclose stale components. Snapshot fields are candidate signals; cite reports/events before making qualitative conclusions.",
        },
    )


def run_fundamental_analysis(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    start_date = str(spec.get("start_date") or "").strip()
    end_date = str(spec.get("end_date") or "").strip()
    periods = clamp_int(spec.get("periods"), 12, 1, 40)
    if not stock_code and not stock_name:
        emit([], {
            "row_count": 0, "read_only": True, "coverage_status": "entity_required",
            "instruction": "Provide stock_code or stock_name; do not infer a company from conversation context.",
        })
        return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH targets AS (
              SELECT asset_id, stock_code, stock_name, industry_name
              FROM analytics.asset_industry_current
              WHERE asset_type = 'stock'
                AND (%(stock_code)s = '' OR stock_code = %(stock_code)s)
                AND (%(stock_name)s = '' OR stock_name ILIKE '%%' || %(stock_name)s || '%%')
              ORDER BY CASE WHEN stock_code = %(stock_code)s OR stock_name = %(stock_name)s THEN 0 ELSE 1 END, stock_code
              LIMIT 5
            ), latest_values AS (
              SELECT f.asset_id, f.report_date, f.metric_name, f.metric_value, f.source,
                     row_number() OVER (
                       PARTITION BY f.asset_id, f.report_date, f.metric_name
                       ORDER BY f.created_at DESC, f.source
                     ) AS rn
              FROM company.financial_metrics f
              JOIN targets t ON t.asset_id = f.asset_id
              WHERE (%(start_date)s = '' OR f.report_date >= %(start_date)s::date)
                AND (%(end_date)s = '' OR f.report_date <= %(end_date)s::date)
            ), pivoted AS (
              SELECT t.stock_code, t.stock_name, t.industry_name, v.report_date,
                     max(v.metric_value) FILTER (WHERE v.metric_name = '营业总收入') AS revenue,
                     max(v.metric_value) FILTER (WHERE v.metric_name = '归母净利润') AS net_profit_parent,
                     max(v.metric_value) FILTER (WHERE v.metric_name = '每股收益') AS eps,
                     max(v.metric_value) FILTER (WHERE v.metric_name = '净资产收益率') AS roe_pct,
                     max(v.metric_value) FILTER (WHERE v.metric_name = '营收同比') AS revenue_yoy_pct,
                     max(v.metric_value) FILTER (WHERE v.metric_name = '净利同比') AS net_profit_yoy_pct,
                     array_agg(DISTINCT v.source ORDER BY v.source) AS sources
              FROM latest_values v
              JOIN targets t ON t.asset_id = v.asset_id
              WHERE v.rn = 1
              GROUP BY t.stock_code, t.stock_name, t.industry_name, v.report_date
            )
            SELECT stock_code, stock_name, industry_name, report_date,
                   round(revenue::numeric, 2) AS revenue,
                   round(net_profit_parent::numeric, 2) AS net_profit_parent,
                   round(eps::numeric, 4) AS eps,
                   round(roe_pct::numeric, 4) AS roe_pct,
                   round(revenue_yoy_pct::numeric, 4) AS revenue_yoy_pct,
                   round(net_profit_yoy_pct::numeric, 4) AS net_profit_yoy_pct,
                   sources
            FROM pivoted
            ORDER BY report_date DESC, stock_code
            LIMIT %(periods)s
            """,
            {
                "stock_code": stock_code, "stock_name": stock_name,
                "start_date": start_date, "end_date": end_date, "periods": periods,
            },
        ).fetchall()
        health = conn.execute(
            """
            SELECT max(report_date) AS dataset_latest_report_date,
                   count(DISTINCT asset_id)::int AS dataset_covered_assets,
                   count(*)::bigint AS dataset_metric_rows,
                   array_agg(DISTINCT metric_name ORDER BY metric_name) AS available_metrics
            FROM company.financial_metrics
            """
        ).fetchone()
        universe = conn.execute(
            "SELECT count(*)::int AS universe_count FROM market.assets WHERE asset_type='stock'"
        ).fetchone()
    covered = int((health or {}).get("dataset_covered_assets") or 0)
    universe_count = int((universe or {}).get("universe_count") or 0)
    emit(rows, {
        "stock_code": stock_code, "stock_name": stock_name,
        "start_date": start_date or None, "end_date": end_date or None,
        "row_count": len(rows), "read_only": True,
        "coverage_status": "local_market_covered" if rows else "not_found_or_no_financials",
        "universe_count": universe_count, "covered_count": covered,
        "coverage_pct": round(100 * covered / universe_count, 2) if universe_count else 0,
        "units": {
            "revenue": "CNY", "net_profit_parent": "CNY", "eps": "CNY/share",
            "roe_pct": "percentage points", "revenue_yoy_pct": "percentage points",
            "net_profit_yoy_pct": "percentage points",
        },
        "metric_definition": "Source-reported company financial statement values. Percentage fields are percentage points, not decimals.",
        "warnings": [
            "Current source exposes six normalized metrics; gross margin and debt ratio are not populated and must remain missing.",
            "A report date identifies a disclosed period; compare like-for-like periods before inferring growth trends.",
        ],
        **(health or {}),
    })


def run_peer_compare(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    industry = str(spec.get("industry") or "").strip()
    peer_codes = source_types_arg(spec.get("peer_stock_codes"))[:30]
    as_of_date = str(spec.get("as_of_date") or "").strip()
    limit = clamp_int(spec.get("limit"), 20, 2, 50)
    if not stock_code and not stock_name and not industry and not peer_codes:
        emit([], {
            "row_count": 0, "read_only": True, "coverage_status": "comparison_scope_required",
            "instruction": "Provide a target stock, industry, or explicit peer_stock_codes.",
        })
        return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH selected_date AS (
              SELECT COALESCE(%(as_of_date)s::date, max(as_of_date)) AS dt
              FROM analytics.asset_snapshot_daily
            ), target AS (
              SELECT a.asset_id, a.stock_code AS symbol, a.stock_name AS name,
                     a.industry_name, a.industry_source, a.industry_source_date
              FROM analytics.asset_industry_current a
              WHERE a.asset_type='stock'
                AND (%(stock_code)s = '' OR a.stock_code=%(stock_code)s)
                AND (%(stock_name)s = '' OR a.stock_name ILIKE '%%' || %(stock_name)s || '%%')
              ORDER BY CASE WHEN a.stock_code=%(stock_code)s OR a.stock_name=%(stock_name)s THEN 0 ELSE 1 END, a.stock_code
              LIMIT 1
            ), scope AS (
              SELECT s.*, a.industry_name, a.industry_source, a.industry_source_date,
                     EXISTS (SELECT 1 FROM target t WHERE t.asset_id=s.asset_id) AS is_target
              FROM analytics.asset_snapshot_daily s
              JOIN selected_date d ON d.dt=s.as_of_date
              JOIN analytics.asset_industry_current a ON a.asset_id=s.asset_id
              WHERE a.asset_type='stock'
                AND (
                  (%(peer_codes)s::text[] <> '{}'::text[] AND s.stock_code=ANY(%(peer_codes)s::text[]))
                  OR (%(industry)s <> '' AND a.industry_name ILIKE '%%' || %(industry)s || '%%')
                  OR (%(peer_codes)s::text[] = '{}'::text[] AND %(industry)s = '' AND EXISTS (
                    SELECT 1 FROM target t WHERE t.industry_name IS NOT NULL AND a.industry_name=t.industry_name
                  ))
                  OR EXISTS (SELECT 1 FROM target t WHERE t.asset_id=s.asset_id)
                )
            ), ranked AS (
              SELECT scope.*,
                     rank() OVER (ORDER BY pe_ttm ASC NULLS LAST) AS pe_rank,
                     rank() OVER (ORDER BY pb ASC NULLS LAST) AS pb_rank,
                     rank() OVER (ORDER BY forecast_eps_growth DESC NULLS LAST) AS forecast_growth_rank,
                     rank() OVER (ORDER BY ret_20d DESC NULLS LAST) AS ret_20d_rank,
                     count(*) OVER()::int AS peer_count
              FROM scope
            )
            SELECT stock_code, stock_name, industry_name, industry_source, industry_source_date,
                   as_of_date, is_target, peer_count,
                   round(close::numeric,3) AS close,
                   round(pe_ttm::numeric,2) AS pe_ttm, pe_rank,
                   round(pb::numeric,2) AS pb, pb_rank,
                   round(forward_pe::numeric,2) AS forward_pe,
                   round(forecast_eps_growth::numeric,4) AS forecast_eps_growth,
                   forecast_growth_rank,
                   round(actual_net_profit_yoy::numeric,4) AS actual_net_profit_yoy,
                   actual_report_period, actual_report_date,
                   round(ret_20d::numeric,4) AS ret_20d, ret_20d_rank,
                   forecast_institution_count_180d, recent_report_count,
                   latest_rating, source_dates
            FROM ranked
            ORDER BY is_target DESC, composite_score DESC NULLS LAST, stock_code
            LIMIT %(limit)s
            """,
            {
                "stock_code": stock_code, "stock_name": stock_name, "industry": industry,
                "peer_codes": peer_codes, "as_of_date": as_of_date or None, "limit": limit,
            },
        ).fetchall()
    peer_count = int(rows[0].get("peer_count") or 0) if rows else 0
    emit(rows, {
        "stock_code": stock_code, "stock_name": stock_name, "industry": industry,
        "requested_peer_codes": peer_codes, "as_of_date": rows[0].get("as_of_date") if rows else as_of_date or None,
        "row_count": len(rows), "peer_universe_count": peer_count, "read_only": True,
        "coverage_status": "comparison_ready" if rows else "no_comparable_rows",
        "rank_direction": {
            "pe_rank": "lower PE ranks first; negative PE remains present and must be interpreted as loss-making",
            "pb_rank": "lower PB ranks first", "forecast_growth_rank": "higher forecast growth ranks first",
            "ret_20d_rank": "higher 20-day return ranks first",
        },
        "warnings": [
            "Industry classification uses market.assets when present and otherwise the latest research-report industry with its evidence date; classifications may still be broad.",
            "Forecast growth coverage is partial; missing forecasts must not be ranked as zero growth.",
        ],
    })


def run_relative_valuation(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    as_of_date = str(spec.get("as_of_date") or "").strip()
    lookback_days = clamp_int(spec.get("lookback_days"), 1095, 30, 3650)
    if not stock_code and not stock_name:
        emit([], {"row_count": 0, "read_only": True, "coverage_status": "entity_required"})
        return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH targets AS (
              SELECT asset_id, stock_code, stock_name, industry_name
              FROM analytics.asset_industry_current
              WHERE asset_type='stock'
                AND (%(stock_code)s='' OR stock_code=%(stock_code)s)
                AND (%(stock_name)s='' OR stock_name ILIKE '%%' || %(stock_name)s || '%%')
              ORDER BY CASE WHEN stock_code=%(stock_code)s OR stock_name=%(stock_name)s THEN 0 ELSE 1 END, stock_code
              LIMIT 3
            ), raw AS (
              SELECT DISTINCT ON (f.asset_id, (f.ts AT TIME ZONE 'Asia/Shanghai')::date, f.factor_name)
                     f.asset_id, (f.ts AT TIME ZONE 'Asia/Shanghai')::date AS dt,
                     f.factor_name, f.factor_value::numeric / 100.0 AS value, f.source
              FROM market.factors_daily f
              JOIN targets t ON t.asset_id=f.asset_id
              WHERE f.factor_name IN ('pe_ttm','pb')
                AND (f.ts AT TIME ZONE 'Asia/Shanghai')::date <= COALESCE(%(as_of_date)s::date, current_date)
                AND (f.ts AT TIME ZONE 'Asia/Shanghai')::date >= COALESCE(%(as_of_date)s::date, current_date) - %(lookback_days)s
              ORDER BY f.asset_id, (f.ts AT TIME ZONE 'Asia/Shanghai')::date, f.factor_name, f.created_at DESC, f.source
            ), daily AS (
              SELECT asset_id, dt,
                     max(value) FILTER (WHERE factor_name='pe_ttm') AS pe_ttm,
                     max(value) FILTER (WHERE factor_name='pb') AS pb,
                     array_agg(DISTINCT source ORDER BY source) AS sources
              FROM raw GROUP BY asset_id, dt
            ), latest AS (
              SELECT DISTINCT ON (asset_id) asset_id, dt, pe_ttm, pb, sources
              FROM daily ORDER BY asset_id, dt DESC
            ), stats AS (
              SELECT asset_id,
                     count(pe_ttm) FILTER (WHERE pe_ttm>0)::int AS pe_observations,
                     percentile_cont(0.25) WITHIN GROUP (ORDER BY pe_ttm) FILTER (WHERE pe_ttm>0) AS pe_p25,
                     percentile_cont(0.5) WITHIN GROUP (ORDER BY pe_ttm) FILTER (WHERE pe_ttm>0) AS pe_median,
                     percentile_cont(0.75) WITHIN GROUP (ORDER BY pe_ttm) FILTER (WHERE pe_ttm>0) AS pe_p75,
                     min(pe_ttm) FILTER (WHERE pe_ttm>0) AS pe_min,
                     max(pe_ttm) FILTER (WHERE pe_ttm>0) AS pe_max,
                     count(pb) FILTER (WHERE pb>0)::int AS pb_observations,
                     percentile_cont(0.25) WITHIN GROUP (ORDER BY pb) FILTER (WHERE pb>0) AS pb_p25,
                     percentile_cont(0.5) WITHIN GROUP (ORDER BY pb) FILTER (WHERE pb>0) AS pb_median,
                     percentile_cont(0.75) WITHIN GROUP (ORDER BY pb) FILTER (WHERE pb>0) AS pb_p75
              FROM daily GROUP BY asset_id
            )
            SELECT t.stock_code, t.stock_name, t.industry_name, l.dt AS as_of_date,
                   round(l.pe_ttm,2) AS pe_ttm,
                   round((100.0 * (SELECT count(*) FROM daily d WHERE d.asset_id=l.asset_id AND d.pe_ttm>0 AND d.pe_ttm<=l.pe_ttm)
                     / NULLIF(s.pe_observations,0))::numeric,2) AS pe_history_percentile_pct,
                   s.pe_observations, round(s.pe_min::numeric,2) AS pe_min,
                   round(s.pe_p25::numeric,2) AS pe_p25, round(s.pe_median::numeric,2) AS pe_median,
                   round(s.pe_p75::numeric,2) AS pe_p75, round(s.pe_max::numeric,2) AS pe_max,
                   round(l.pb,2) AS pb,
                   round((100.0 * (SELECT count(*) FROM daily d WHERE d.asset_id=l.asset_id AND d.pb>0 AND d.pb<=l.pb)
                     / NULLIF(s.pb_observations,0))::numeric,2) AS pb_history_percentile_pct,
                   s.pb_observations, round(s.pb_p25::numeric,2) AS pb_p25,
                   round(s.pb_median::numeric,2) AS pb_median, round(s.pb_p75::numeric,2) AS pb_p75,
                   l.sources
            FROM latest l JOIN stats s USING(asset_id) JOIN targets t USING(asset_id)
            ORDER BY t.stock_code
            """,
            {
                "stock_code": stock_code, "stock_name": stock_name,
                "as_of_date": as_of_date or None, "lookback_days": lookback_days,
            },
        ).fetchall()
    warnings = [
        "Historical PE percentile excludes non-positive PE observations because losses are not comparable valuation multiples.",
        "A low historical percentile is descriptive, not an investment recommendation.",
    ]
    if any(min(int(row.get("pe_observations") or 0), int(row.get("pb_observations") or 0)) < 30 for row in rows):
        warnings.append("Valuation history has fewer than 30 positive observations; percentile is statistically insufficient and must not support a conclusion.")
    emit(rows, {
        "stock_code": stock_code, "stock_name": stock_name, "lookback_days": lookback_days,
        "requested_as_of_date": as_of_date or None, "row_count": len(rows), "read_only": True,
        "coverage_status": "valuation_history_available" if rows else "no_valuation_history",
        "metric_definition": "PE TTM and PB are normalized from market.factors_daily fixed-point values divided by 100.",
        "warnings": warnings,
    })


def run_capital_flow_analysis(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    days = clamp_int(spec.get("days"), 30, 1, 365)
    limit = clamp_int(spec.get("limit"), 60, 1, 150)
    allowed_categories = {"dragon_tiger", "block_trade", "margin_trade", "unlock", "survey", "northbound"}
    categories = [x for x in source_types_arg(spec.get("categories")) if x in allowed_categories]
    if not categories:
        categories = sorted(allowed_categories)
    if not stock_code and not stock_name:
        emit([], {
            "row_count": 0, "read_only": True, "coverage_status": "entity_required",
            "instruction": "Provide stock_code or stock_name. Full-market capital-flow ranking belongs in a separate screen tool.",
        })
        return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH targets AS (
              SELECT asset_id, symbol AS stock_code, name AS stock_name
              FROM market.assets
              WHERE (%(stock_code)s='' OR symbol=%(stock_code)s)
                AND (%(stock_name)s='' OR name ILIKE '%%' || %(stock_name)s || '%%')
              ORDER BY CASE WHEN symbol=%(stock_code)s OR name=%(stock_name)s THEN 0 ELSE 1 END,
                       CASE WHEN asset_type='stock' THEN 0 ELSE 1 END, symbol
              LIMIT 8
            ), records AS (
              SELECT 'money_flow'::text AS record_type, f.ts AS record_date,
                     t.stock_code, t.stock_name, 'money_flow'::text AS category,
                     f.main_net, f.small_net, f.mid_net, f.big_net, f.super_net,
                     jsonb_build_object('source', f.source) AS details
              FROM market.money_flow_daily f JOIN targets t USING(asset_id)
              WHERE f.ts >= current_date - %(days)s
              UNION ALL
              SELECT 'market_event', e.event_date, t.stock_code, t.stock_name, e.category,
                     NULL::numeric, NULL::numeric, NULL::numeric, NULL::numeric, NULL::numeric,
                     CASE e.category
                       WHEN 'dragon_tiger' THEN jsonb_build_object(
                         'reason', e.raw_payload->>'EXPLANATION', 'explain', e.raw_payload->>'EXPLAIN',
                         'close', e.raw_payload->'CLOSE_PRICE', 'change_rate_pct', e.raw_payload->'CHANGE_RATE',
                         'buy_amount', e.raw_payload->'BILLBOARD_BUY_AMT', 'sell_amount', e.raw_payload->'BILLBOARD_SELL_AMT',
                         'net_amount', e.raw_payload->'BILLBOARD_NET_AMT', 'turnover_rate_pct', e.raw_payload->'TURNOVERRATE'
                       )
                       WHEN 'block_trade' THEN jsonb_build_object(
                         'deal_amount', e.raw_payload->'DEAL_AMT', 'deal_volume', e.raw_payload->'DEAL_VOLUME',
                         'deal_price', e.raw_payload->'DEAL_PRICE', 'close_price', e.raw_payload->'CLOSE_PRICE',
                         'premium_ratio', e.raw_payload->'PREMIUM_RATIO', 'discount_ratio', e.raw_payload->'DISCOUNT_RATIO',
                         'buyer', e.raw_payload->>'BUYER_NAME', 'seller', e.raw_payload->>'SELLER_NAME'
                       )
                       ELSE jsonb_build_object('source', e.source, 'source_hash', e.source_hash)
                     END AS details
              FROM market.events_raw e JOIN targets t USING(asset_id)
              WHERE e.category=ANY(%(categories)s::text[]) AND e.event_date >= current_date - %(days)s
            )
            SELECT record_type, record_date, stock_code, stock_name, category,
                   round(main_net::numeric,2) AS main_net,
                   round(small_net::numeric,2) AS small_net,
                   round(mid_net::numeric,2) AS mid_net,
                   round(big_net::numeric,2) AS big_net,
                   round(super_net::numeric,2) AS super_net,
                   details
            FROM records
            ORDER BY CASE WHEN record_type='market_event' THEN 0 ELSE 1 END,
                     record_date DESC, category
            LIMIT %(limit)s
            """,
            {
                "stock_code": stock_code, "stock_name": stock_name, "days": days,
                "categories": categories, "limit": limit,
            },
        ).fetchall()
        coverage = conn.execute(
            """
            SELECT 'money_flow' AS category, max(ts)::date AS latest_date, count(*)::bigint AS row_count
            FROM market.money_flow_daily
            UNION ALL
            SELECT category, max(event_date), count(*)::bigint
            FROM market.events_raw WHERE category=ANY(%(categories)s::text[]) GROUP BY category
            ORDER BY category
            """,
            {"categories": categories},
        ).fetchall()
    populated = {row.get("category") for row in coverage if int(row.get("row_count") or 0) > 0}
    emit(rows, {
        "stock_code": stock_code, "stock_name": stock_name, "days": days,
        "requested_categories": categories, "populated_categories": sorted(populated),
        "missing_categories": sorted(set(categories) - populated),
        "dataset_coverage": coverage, "row_count": len(rows), "read_only": True,
        "coverage_status": "partial_categories" if set(categories) - populated else "requested_categories_available",
        "units": {"money_flow_fields": "CNY", "event_details": "source-specific; percentage fields are percentage points or ratios as named"},
        "warnings": [
            "Capital-flow classifications are source-defined order-size buckets and do not prove institutional intent.",
            "Raw market-event categories can have different watermarks; use dataset_coverage dates before comparison.",
        ],
    })


def run_macro_context(root: str, spec: dict[str, Any]) -> None:
    indicators = source_types_arg(spec.get("indicators"))[:20]
    start_date = str(spec.get("start_date") or "").strip()
    end_date = str(spec.get("end_date") or "").strip()
    periods = clamp_int(spec.get("periods"), 12, 1, 120)
    patterns = [f"%{item}%" for item in indicators]
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH filtered AS (
              SELECT i.indicator_code, i.indicator_name, v.period, v.observed_at,
                     v.value, v.source, v.raw_payload,
                     row_number() OVER (
                       PARTITION BY i.indicator_code ORDER BY v.observed_at DESC NULLS LAST, v.period DESC
                     ) AS rn
              FROM macro.indicators i
              JOIN macro.indicator_values v USING(indicator_id)
              WHERE (%(patterns)s::text[] = '{}'::text[] OR i.indicator_code ILIKE ANY(%(patterns)s) OR i.indicator_name ILIKE ANY(%(patterns)s))
                AND (%(start_date)s='' OR v.observed_at >= %(start_date)s::date)
                AND (%(end_date)s='' OR v.observed_at <= %(end_date)s::date)
            )
            SELECT indicator_code, indicator_name, period, observed_at,
                   round(value::numeric,6) AS value, source,
                   CASE indicator_code
                     WHEN 'CPI' THEN 'national_yoy'
                     WHEN 'PPI' THEN 'producer_price_yoy'
                     WHEN 'GDP' THEN 'gdp_yoy'
                     WHEN 'PMI' THEN 'manufacturing_pmi'
                     WHEN 'MONEY_SUPPLY' THEN 'm2_yoy'
                     WHEN 'DEPOSIT_RATE' THEN 'benchmark_deposit_rate'
                     ELSE 'source_primary_value'
                   END AS value_definition,
                   CASE indicator_code
                     WHEN 'PMI' THEN 'index'
                     WHEN 'GDP' THEN 'percentage points'
                     ELSE 'percentage points'
                   END AS source_unit,
                   CASE indicator_code
                     WHEN 'CPI' THEN jsonb_build_object('mom', raw_payload->'NATIONAL_SEQUENTIAL', 'index', raw_payload->'NATIONAL_BASE')
                     WHEN 'PMI' THEN jsonb_build_object('non_manufacturing_pmi', raw_payload->'NMAKE_INDEX')
                     WHEN 'GDP' THEN jsonb_build_object('gdp_100m_cny', raw_payload->'DOMESTICL_PRODUCT_BASE')
                     WHEN 'MONEY_SUPPLY' THEN jsonb_build_object('m2_100m_cny', raw_payload->'BASIC_CURRENCY')
                     ELSE '{}'::jsonb
                   END AS secondary_values
            FROM filtered WHERE rn <= %(periods)s
            ORDER BY indicator_code, observed_at DESC NULLS LAST, period DESC
            """,
            {
                "patterns": patterns, "start_date": start_date, "end_date": end_date,
                "periods": periods,
            },
        ).fetchall()
        health = conn.execute(
            """
            SELECT count(DISTINCT i.indicator_code)::int AS indicator_count,
                   max(v.observed_at) AS dataset_latest_observation,
                   count(*)::bigint AS dataset_value_rows
            FROM macro.indicators i JOIN macro.indicator_values v USING(indicator_id)
            """
        ).fetchone()
    emit(rows, {
        "requested_indicators": indicators, "start_date": start_date or None, "end_date": end_date or None,
        "periods_per_indicator": periods, "row_count": len(rows), "read_only": True,
        "coverage_status": "macro_data_available" if rows else "no_matching_macro_data",
        "metric_definition": "Macro values are source observations; units remain source-specific and must be read from source_unit/raw metadata.",
        "warnings": [
            "Publication dates can lag observed periods. Do not use a value in historical analysis before it was publicly available.",
            "Different indicators have different frequencies and revision policies; do not compare levels without checking units.",
        ],
        **(health or {}),
    })


def run_entity_resolve(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("query") or "").strip()
    asset_type = str(spec.get("asset_type") or "").strip().lower()
    # External schemas/models commonly use equity for a stock. Do not turn a
    # known company into a false data gap because of this vocabulary difference.
    asset_type = {"equity": "stock", "equities": "stock", "股票": "stock"}.get(asset_type, asset_type)
    limit = clamp_int(spec.get("limit"), 10, 1, 30)
    if not query:
        emit([], {"query": query, "row_count": 0, "read_only": True, "coverage_status": "empty_query"})
        return
    pattern = f"%{query}%"
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH candidates AS (
              SELECT 'asset'::text AS entity_type, a.asset_id::text AS entity_id,
                     a.symbol, a.name AS canonical_name, a.asset_type AS subtype,
                     a.exchange, a.industry_name, ARRAY[]::text[] AS aliases,
                     CASE WHEN lower(a.symbol) = lower(%(query)s) THEN 100
                          WHEN lower(a.name) = lower(%(query)s) THEN 98
                          WHEN a.symbol ILIKE %(pattern)s THEN 85
                          WHEN a.name ILIKE %(pattern)s THEN 80
                          ELSE greatest(similarity(coalesce(a.name, ''), %(query)s),
                                        similarity(coalesce(a.symbol, ''), %(query)s)) * 70 END AS match_score
              FROM market.assets a
              WHERE (%(asset_type)s = '' OR a.asset_type = %(asset_type)s)
                AND (
                  a.symbol ILIKE %(pattern)s OR a.name ILIKE %(pattern)s
                  OR similarity(coalesce(a.name, ''), %(query)s) > 0.25
                )
              UNION ALL
              SELECT 'concept', c.concept_id::text, NULL, c.name, c.concept_type,
                     NULL, NULL, c.aliases,
                     CASE WHEN lower(c.name) = lower(%(query)s) OR lower(c.normalized_name) = lower(%(query)s) THEN 100
                          WHEN EXISTS (SELECT 1 FROM unnest(c.aliases) x WHERE lower(x) = lower(%(query)s)) THEN 96
                          WHEN c.name ILIKE %(pattern)s THEN 82 ELSE 75 END
              FROM kg.concepts c
              WHERE c.status = 'active'
                AND (
                  c.name ILIKE %(pattern)s OR c.normalized_name ILIKE %(pattern)s
                  OR EXISTS (SELECT 1 FROM unnest(c.aliases) x WHERE x ILIKE %(pattern)s)
                )
            )
            SELECT entity_type, entity_id, symbol, canonical_name, subtype,
                   exchange, industry_name, aliases, round(match_score::numeric, 2) AS match_score
            FROM candidates
            ORDER BY match_score DESC, entity_type, canonical_name
            LIMIT %(limit)s
            """,
            {"query": query, "pattern": pattern, "asset_type": asset_type, "limit": limit},
        ).fetchall()
    has_assets = any(row.get("entity_type") == "asset" for row in rows)
    has_concepts = any(row.get("entity_type") == "concept" for row in rows)
    ticker_like = bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,9}", query))
    if has_assets:
        coverage_status = "local_market_covered"
        recommended_route = "use stock_code/entity_id with finance snapshot or screen tools"
    elif has_concepts:
        coverage_status = "local_concept_covered"
        recommended_route = "use canonical_name and aliases with finance_document_retrieve or theme tools"
    elif ticker_like:
        coverage_status = "unsupported_external_market"
        recommended_route = "external market-data connector is not configured; do not infer prices or fundamentals"
    else:
        coverage_status = "not_found_local"
        recommended_route = "retry once with an exact stock code, company full name, or narrower concept"
    emit(
        rows,
        {
            "query": query,
            "asset_type": asset_type,
            "row_count": len(rows),
            "read_only": True,
            "coverage_status": coverage_status,
            "recommended_route": recommended_route,
        },
    )


def run_data_context(root: str, spec: dict[str, Any]) -> None:
    intent = str(spec.get("intent") or "").strip()
    metric_keys = source_types_arg(spec.get("metric_keys"))
    dataset_keys = source_types_arg(spec.get("dataset_keys"))
    query_keys = source_types_arg(spec.get("query_keys"))
    include_health = bool(spec.get("include_health", True))
    limit = clamp_int(spec.get("limit"), 30, 1, 100)
    pattern = f"%{intent}%" if intent else "%%"
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        catalog_rows = conn.execute(
            """
            SELECT 'metric' AS context_type, metric_key AS context_key, display_name AS title,
                   description, canonical_relation, grain, time_column,
                   freshness_slo::text, null_policy, lookahead_risk,
                   jsonb_build_object(
                     'value_columns', value_columns,
                     'dimension_columns', dimension_columns,
                     'unit', unit,
                     'formula', formula,
                     'metadata', metadata
                   ) AS details
            FROM agent.metric_catalog
            WHERE status IN ('active', 'experimental')
              AND (
                (%(has_metric_keys)s = true AND (metric_key = ANY(%(metric_keys)s) OR value_columns && %(metric_keys)s))
                OR
                (%(has_metric_keys)s = false AND (%(intent)s = '' OR metric_key ILIKE %(pattern)s OR display_name ILIKE %(pattern)s OR description ILIKE %(pattern)s))
              )
            UNION ALL
            SELECT 'query', query_key, intent, description,
                   array_to_string(source_relations, ', '), NULL, NULL,
                   freshness_requirements, NULL, NULL,
                   jsonb_build_object(
                     'tool_name', tool_name,
                     'input_schema', input_schema,
                     'output_schema', output_schema,
                     'evidence_requirements', evidence_requirements,
                     'safety_notes', safety_notes
                   )
            FROM agent.query_catalog
            WHERE status IN ('active', 'experimental')
              AND (%(has_query_keys)s = false OR query_key = ANY(%(query_keys)s))
              AND (%(intent)s = '' OR query_key ILIKE %(pattern)s OR intent ILIKE %(pattern)s OR description ILIKE %(pattern)s)
            UNION ALL
            SELECT 'contract', dataset_key, relation_name, grain,
                   relation_name, grain, watermark_column,
                   freshness_slo::text, NULL, NULL,
                   jsonb_build_object(
                     'primary_key_columns', primary_key_columns,
                     'required_columns', required_columns,
                     'quality_checks', quality_checks,
                     'owner', owner
                   )
            FROM agent.data_contracts
            WHERE status IN ('active', 'experimental')
              AND (%(has_dataset_keys)s = false OR dataset_key = ANY(%(dataset_keys)s))
              AND (%(intent)s = '' OR dataset_key ILIKE %(pattern)s OR relation_name ILIKE %(pattern)s OR grain ILIKE %(pattern)s)
            ORDER BY context_type, context_key
            LIMIT %(limit)s
            """,
            {
                "intent": intent,
                "pattern": pattern,
                "has_metric_keys": bool(metric_keys),
                "metric_keys": metric_keys,
                "has_query_keys": bool(query_keys),
                "query_keys": query_keys,
                "has_dataset_keys": bool(dataset_keys),
                "dataset_keys": dataset_keys,
                "limit": limit,
            },
        ).fetchall()
        health_rows = []
        if include_health:
            if str(root) not in sys.path: sys.path.insert(0, str(root))
            from scripts.analytics.data_catalog import load_data_catalog
            registered = load_data_catalog(conn, dt.datetime.now(dt.timezone.utc), dataset_keys, limit, intent)
            health_rows = [{"context_type":"registered_dataset", "context_key":r['dataset_key'],
                "title":r['title'], "description":r['usage_contract'],
                "canonical_relation":r['canonical_relation'], "grain":r['grain'],
                "details":r} for r in registered]

    rows = catalog_rows + health_rows
    emit(
        rows,
        {
            "intent": intent,
            "metric_keys": metric_keys,
            "dataset_keys": dataset_keys,
            "query_keys": query_keys,
            "include_health": include_health,
            "row_count": len(rows),
            "read_only": True,
            "instruction": "Use canonical_relation, grain, time_column, lookahead_risk, and health details to state metric scope and freshness.",
        },
    )


def run_market_screen(root: str, spec: dict[str, Any]) -> None:
    scope = str(spec.get("scope") or "all").strip().lower()
    theme = str(spec.get("theme") or "").strip()
    if theme:
        scope = "theme"
    if scope not in {"all", "theme"}:
        scope = "all"
    as_of_date = str(spec.get("as_of_date") or "").strip()
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_codes = _v3_codes(spec)
    stock_name = str(spec.get("stock_name") or "").strip()
    industry = str(spec.get("industry") or "").strip()
    industry_terms = {
        "食品饮料": ["食品", "饮料", "乳品", "白酒", "酿酒"],
        "电力公用事业": ["电力", "公用事业"],
        "玻纤": ["玻纤", "玻璃纤维"],
        "电子布": ["玻纤", "玻璃纤维"],
    }.get(industry, [industry])
    pe_ttm_lt = as_float(spec.get("pe_ttm_lt"))
    pe_ttm_gt = as_float(spec.get("pe_ttm_gt"))
    pb_lt = as_float(spec.get("pb_lt"))
    pb_gt = as_float(spec.get("pb_gt"))
    forward_pe_lt = as_float(spec.get("forward_pe_lt"))
    forward_pe_gt = as_float(spec.get("forward_pe_gt"))
    forecast_growth_gte = normalize_pct_threshold(spec.get("forecast_growth_gte"))
    forecast_growth_lte = normalize_pct_threshold(spec.get("forecast_growth_lte"))
    actual_growth_gte = normalize_pct_threshold(spec.get("actual_profit_growth_gte"))
    actual_growth_lte = normalize_pct_threshold(spec.get("actual_profit_growth_lte"))
    ret_5d_gte = normalize_pct_threshold(spec.get("ret_5d_gte"))
    ret_5d_lte = normalize_pct_threshold(spec.get("ret_5d_lte"))
    ret_20d_gte = normalize_pct_threshold(spec.get("ret_20d_gte"))
    ret_20d_lte = normalize_pct_threshold(spec.get("ret_20d_lte"))
    min_reports = clamp_int(spec.get("min_reports"), 0, 0, 10000)
    min_positive = clamp_int(spec.get("min_positive_ratings"), 0, 0, 10000)
    min_upgrades = clamp_int(spec.get("min_upgrades"), 0, 0, 10000)
    factor_filters = []
    for item in (spec.get("factor_filters") or [])[:5]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("factor_name") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,100}", name):
            continue
        factor_filters.append({
            "factor_name": name,
            "percentile_gte": normalize_pct_threshold(item.get("percentile_gte")),
            "percentile_lte": normalize_pct_threshold(item.get("percentile_lte")),
            "zscore_gte": as_float(item.get("zscore_gte")),
            "zscore_lte": as_float(item.get("zscore_lte")),
        })
    factor_match = str(spec.get("factor_match") or "all").strip().lower()
    factor_match = "any" if factor_match == "any" else "all"
    factor_lifecycles = factor_lifecycle_arg(spec.get("factor_lifecycle"), False)
    include_negative_pe = bool(spec.get("include_negative_pe", False))
    pe_filter = pe_ttm_lt is not None or pe_ttm_gt is not None
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    sort_by = str(spec.get("sort_by") or "composite_score").strip().lower()
    sort_columns = {
        "composite_score": "s.composite_score",
        "pe_ttm": "s.pe_ttm",
        "pb": "s.pb",
        "forward_pe": "s.forward_pe",
        "forecast_growth": "s.forecast_eps_growth",
        "actual_profit_growth": "s.actual_net_profit_yoy",
        "ret_5d": "s.ret_5d",
        "ret_20d": "s.ret_20d",
        "recent_reports": "s.recent_report_count",
        "factor_percentile": "s.primary_factor_percentile",
    }
    sort_expression = sort_columns.get(sort_by, sort_columns["composite_score"])
    default_direction = "asc" if sort_by in {"pe_ttm", "pb", "forward_pe"} else "desc"
    sort_direction = str(spec.get("sort_direction") or default_direction).strip().lower()
    sort_direction = "ASC" if sort_direction == "asc" else "DESC"
    theme_patterns = [f"%{term}%" for term in theme_terms(theme)] or ["%%"]

    sql = f"""
        WITH selected_date AS (
          SELECT max(as_of_date) AS dt
          FROM analytics.asset_snapshot_daily
          WHERE (%(as_of_date)s::date IS NULL OR as_of_date <= %(as_of_date)s::date)
        ),
        industry_at_date AS (
          SELECT DISTINCT ON (r.stock_code) r.stock_code,r.industry_name,r.report_date
          FROM research.reports_resolved r CROSS JOIN selected_date sd
          WHERE r.stock_code IS NOT NULL AND nullif(r.industry_name,'') IS NOT NULL
            AND r.report_date <= sd.dt
          ORDER BY r.stock_code,r.report_date DESC,r.updated_at DESC,r.document_id DESC
        ),
        requested_factors AS (
          SELECT ord::int AS ord,value->>'factor_name' AS factor_name,
                 nullif(value->>'percentile_gte','')::float8 AS percentile_gte,
                 nullif(value->>'percentile_lte','')::float8 AS percentile_lte,
                 nullif(value->>'zscore_gte','')::float8 AS zscore_gte,
                 nullif(value->>'zscore_lte','')::float8 AS zscore_lte
          FROM jsonb_array_elements(%(factor_filters)s::jsonb) WITH ORDINALITY AS x(value,ord)
        ), eligible_factors AS (
          SELECT r.*,d.factor_id,d.display_name,d.lifecycle_status
          FROM requested_factors r JOIN factor.definitions d USING(factor_name)
          WHERE d.lifecycle_status=ANY(%(factor_lifecycles)s)
        ), factor_date AS (
          SELECT %(factor_score_date)s::date AS dt
        ), factor_values AS (
          SELECT fs.asset_id,ef.ord,ef.factor_name,ef.display_name,ef.lifecycle_status,
                 fs.percentile,fs.zscore,fs.raw_value,fs.available_at,fs.source_feature_date,
                 ((ef.percentile_gte IS NULL OR fs.percentile>=ef.percentile_gte)
                  AND (ef.percentile_lte IS NULL OR fs.percentile<=ef.percentile_lte)
                  AND (ef.zscore_gte IS NULL OR fs.zscore>=ef.zscore_gte)
                  AND (ef.zscore_lte IS NULL OR fs.zscore<=ef.zscore_lte)) AS passed
          FROM factor.valid_daily_scores_v1 fs JOIN eligible_factors ef USING(factor_id)
          WHERE fs.dt=%(factor_score_date)s::date
        ), factor_match AS (
          SELECT asset_id,
                 jsonb_object_agg(factor_name,jsonb_build_object(
                   'display_name',display_name,'lifecycle_status',lifecycle_status,
                   'percentile',round(percentile::numeric,6),'zscore',round(zscore::numeric,6),
                   'raw_value',round(raw_value::numeric,6),'available_at',available_at,
                   'source_feature_date',source_feature_date,'passed',passed
                 ) ORDER BY ord) AS factor_values,
                 max(percentile) FILTER (WHERE ord=1) AS primary_factor_percentile,
                 count(*) FILTER (WHERE passed)::int AS passed_factor_count
          FROM factor_values
          GROUP BY asset_id
          HAVING (%(factor_match)s='all' AND count(*) FILTER (WHERE passed)=(SELECT count(*) FROM eligible_factors))
              OR (%(factor_match)s='any' AND count(*) FILTER (WHERE passed)>0)
        ),
        theme_match AS (
          SELECT e.stock_code, array_agg(DISTINCT c.name ORDER BY c.name) AS matched_themes,
                 max(e.confidence) AS theme_confidence,
                 count(*)::int AS theme_evidence_count
          FROM kg.company_concept_exposures e
          JOIN kg.concepts c ON c.concept_id=e.concept_id
          WHERE e.status='active' AND c.status='active'
            AND e.relation_type <> 'weak_mention'
            AND (c.name ILIKE ANY(%(theme_patterns)s) OR array_to_string(c.aliases, ',') ILIKE ANY(%(theme_patterns)s))
          GROUP BY e.stock_code
        ),
        filtered AS (
          SELECT s.*, i.industry_name,i.industry_source,i.industry_source_date,
                 tm.matched_themes, tm.theme_confidence, tm.theme_evidence_count,
                 fm.factor_values,fm.primary_factor_percentile,fm.passed_factor_count,fd.dt AS factor_score_date
          FROM analytics.asset_snapshot_daily s
          JOIN selected_date d ON s.as_of_date=d.dt
          CROSS JOIN factor_date fd
          LEFT JOIN market.assets a ON a.asset_id=s.asset_id
          LEFT JOIN industry_at_date ri ON ri.stock_code=s.stock_code
          CROSS JOIN LATERAL (
            SELECT coalesce(CASE WHEN a.updated_at::date<=s.as_of_date THEN nullif(a.industry_name,'') END,
                            ri.industry_name) AS industry_name,
                   CASE WHEN a.updated_at::date<=s.as_of_date AND nullif(a.industry_name,'') IS NOT NULL
                        THEN 'market.assets' ELSE 'dated_research_report' END AS industry_source,
                   CASE WHEN a.updated_at::date<=s.as_of_date AND nullif(a.industry_name,'') IS NOT NULL
                        THEN a.updated_at::date ELSE ri.report_date END AS industry_source_date
          ) i
          LEFT JOIN theme_match tm ON tm.stock_code=s.stock_code
          LEFT JOIN factor_match fm ON fm.asset_id=s.asset_id
          WHERE (%(stock_code)s='' OR s.stock_code=%(stock_code)s)
            AND (%(has_stock_codes)s=false OR s.stock_code=ANY(%(stock_codes)s))
            AND (%(stock_name)s='' OR s.stock_name ILIKE '%%' || %(stock_name)s || '%%')
            AND (%(industry)s='' OR i.industry_name ILIKE ANY(%(industry_patterns)s))
            AND (%(scope)s <> 'theme' OR tm.stock_code IS NOT NULL)
            AND (%(has_factor_filters)s=false OR fm.asset_id IS NOT NULL)
            AND (%(pe_ttm_lt)s::float8 IS NULL OR s.pe_ttm < %(pe_ttm_lt)s)
            AND (%(pe_ttm_gt)s::float8 IS NULL OR s.pe_ttm > %(pe_ttm_gt)s)
            AND (%(pe_filter)s=false OR %(include_negative_pe)s=true OR s.pe_ttm > 0)
            AND (%(pb_lt)s::float8 IS NULL OR s.pb < %(pb_lt)s)
            AND (%(pb_gt)s::float8 IS NULL OR s.pb > %(pb_gt)s)
            AND (%(forward_pe_lt)s::float8 IS NULL OR s.forward_pe < %(forward_pe_lt)s)
            AND (%(forward_pe_gt)s::float8 IS NULL OR s.forward_pe > %(forward_pe_gt)s)
            AND (%(forecast_growth_gte)s::float8 IS NULL OR s.forecast_eps_growth >= %(forecast_growth_gte)s)
            AND (%(forecast_growth_lte)s::float8 IS NULL OR s.forecast_eps_growth <= %(forecast_growth_lte)s)
            AND (%(actual_growth_gte)s::float8 IS NULL OR s.actual_net_profit_yoy >= %(actual_growth_gte)s)
            AND (%(actual_growth_lte)s::float8 IS NULL OR s.actual_net_profit_yoy <= %(actual_growth_lte)s)
            AND (%(ret_5d_gte)s::float8 IS NULL OR s.ret_5d >= %(ret_5d_gte)s)
            AND (%(ret_5d_lte)s::float8 IS NULL OR s.ret_5d <= %(ret_5d_lte)s)
            AND (%(ret_20d_gte)s::float8 IS NULL OR s.ret_20d >= %(ret_20d_gte)s)
            AND (%(ret_20d_lte)s::float8 IS NULL OR s.ret_20d <= %(ret_20d_lte)s)
            AND coalesce(s.recent_report_count,0) >= %(min_reports)s
            AND coalesce(s.positive_rating_count,0) >= %(min_positive)s
            AND coalesce(s.upgrade_count,0) >= %(min_upgrades)s
        )
        SELECT stock_code, stock_name, industry_name,industry_source,industry_source_date,as_of_date,
               source_dates->>'valuation_date' AS valuation_date,
               technical_date, research_date, forecast_date, signal_date,
               round(close::numeric,3) AS close,
               round(pe_ttm::numeric,2) AS pe_ttm, round(pb::numeric,2) AS pb,
               round(forward_pe::numeric,2) AS forward_pe,
               round(forecast_eps_growth::numeric,4) AS forecast_eps_growth,
               actual_report_date, actual_report_period,
               round(actual_net_profit_yoy::numeric,4) AS actual_net_profit_yoy,
               round(ret_5d::numeric,4) AS ret_5d, round(ret_20d::numeric,4) AS ret_20d,
               recent_report_count, positive_rating_count, upgrade_count,
               latest_rating, latest_rating_change,
               round(composite_score::numeric,3) AS composite_score,
               matched_themes, round(theme_confidence::numeric,3) AS theme_confidence,
               theme_evidence_count, pattern_tags, source_dates,
               factor_score_date,factor_values,passed_factor_count,
               count(*) OVER()::int AS strict_candidate_count
        FROM filtered s
        ORDER BY {sort_expression} {sort_direction} NULLS LAST, s.stock_code
        LIMIT %(limit)s
    """
    if not factor_filters:
        # Do not aggregate historical factor cohorts for a price/fundamental
        # screen that requested no factor conditions.
        start=sql.index("        requested_factors AS (")
        end=sql.index("        theme_match AS (",start)
        sql=sql[:start]+"""        factor_date AS (SELECT NULL::date AS dt),
        factor_match AS (SELECT NULL::uuid AS asset_id,NULL::jsonb AS factor_values,
          NULL::float8 AS primary_factor_percentile,NULL::int AS passed_factor_count WHERE false),
"""+sql[end:]
    params = {
        "scope": scope, "theme_patterns": theme_patterns, "as_of_date": as_of_date or None,
        "factor_filters": json.dumps(factor_filters), "factor_lifecycles": factor_lifecycles,
        "factor_match": factor_match, "has_factor_filters": bool(factor_filters),
        "stock_code": stock_code, "stock_codes":stock_codes,"has_stock_codes":bool(stock_codes),
        "stock_name": stock_name, "industry": industry,
        "industry_patterns":[f"%{term}%" for term in industry_terms],
        "pe_ttm_lt": pe_ttm_lt, "pe_ttm_gt": pe_ttm_gt, "pe_filter": pe_filter,
        "include_negative_pe": include_negative_pe, "pb_lt": pb_lt, "pb_gt": pb_gt,
        "forward_pe_lt": forward_pe_lt, "forward_pe_gt": forward_pe_gt,
        "forecast_growth_gte": forecast_growth_gte, "forecast_growth_lte": forecast_growth_lte,
        "actual_growth_gte": actual_growth_gte, "actual_growth_lte": actual_growth_lte,
        "ret_5d_gte": ret_5d_gte, "ret_5d_lte": ret_5d_lte,
        "ret_20d_gte": ret_20d_gte, "ret_20d_lte": ret_20d_lte,
        "min_reports": min_reports, "min_positive": min_positive, "min_upgrades": min_upgrades,
        "limit": limit, "factor_score_date": None,
    }
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        factor_coverage = {}
        if factor_filters:
            # Bind concrete dates before reading the grouped valid-score view.
            # A date derived through a JOIN/group can force a whole-history
            # cohort aggregate. The same selected date serves results/coverage.
            screen_date = conn.execute("""SELECT max(as_of_date) AS dt
                FROM analytics.asset_snapshot_daily
                WHERE (%s::date IS NULL OR as_of_date<=%s::date)""",
                (as_of_date or None, as_of_date or None)).fetchone()['dt']
            requested_names = list(dict.fromkeys(f['factor_name'] for f in factor_filters))
            eligible = conn.execute("""SELECT factor_id,factor_name FROM factor.definitions
                WHERE lifecycle_status=ANY(%s) AND factor_name=ANY(%s)""",
                (factor_lifecycles, requested_names)).fetchall()
            eligible_ids = [f['factor_id'] for f in eligible]
            available_names = {f['factor_name'] for f in eligible}
            factor_coverage = {
                'snapshot_date':screen_date, 'factor_score_date':None,
                'requested_factor_count':len(requested_names), 'eligible_factor_count':len(eligible_ids),
                'unavailable_factors':[name for name in requested_names if name not in available_names],
                'factor_scored_assets':0,
            }
            if screen_date and eligible_ids:
                common = conn.execute("""SELECT dt FROM factor.valid_daily_scores_v1
                    WHERE dt BETWEEN %s::date-31 AND %s::date AND factor_id=ANY(%s)
                      AND available_at<=((%s::date::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai')
                    GROUP BY dt HAVING count(DISTINCT factor_id)=%s
                    ORDER BY dt DESC LIMIT 1""",
                    (screen_date,screen_date,eligible_ids,screen_date,len(eligible_ids))).fetchone()
                if common:
                    params['factor_score_date'] = common['dt']
                    factor_coverage['factor_score_date'] = common['dt']
                    factor_coverage['factor_scored_assets'] = conn.execute("""SELECT count(DISTINCT asset_id)::int AS n
                        FROM factor.valid_daily_scores_v1 WHERE dt=%s AND factor_id=ANY(%s)""",
                        (common['dt'],eligible_ids)).fetchone()['n']
            if not params['factor_score_date']:
                factor_coverage['empty_reason'] = ('unavailable_or_disallowed_factors' if not eligible_ids
                    else 'no_common_current_contract_score_date')
        # Without any eligible score date the factor predicate cannot match.
        # Return a reason and ordinary snapshot coverage, not old scores or a
        # costly full-universe screen known in advance to be empty.
        rows = [] if factor_filters and not params['factor_score_date'] else conn.execute(sql, params).fetchall()
        coverage = conn.execute(
            """
            WITH selected_date AS (
              SELECT max(as_of_date) AS dt
              FROM analytics.asset_snapshot_daily
              WHERE (%(as_of_date)s::date IS NULL OR as_of_date <= %(as_of_date)s::date)
            )
            SELECT d.dt AS as_of_date,
                   count(*)::int AS universe_count,
                   count(pe_ttm)::int AS pe_ttm_covered,
                   count(pb)::int AS pb_covered,
                   count(forward_pe)::int AS forward_pe_covered,
                   count(forecast_eps_growth)::int AS forecast_growth_covered,
                   count(actual_net_profit_yoy)::int AS actual_profit_growth_covered,
                   count(ret_5d)::int AS ret_5d_covered,
                   count(ret_20d)::int AS ret_20d_covered,
                   max(technical_date) AS latest_technical_date,
                   max(research_date) AS latest_research_date,
                   max(forecast_date) AS latest_forecast_date
            FROM analytics.asset_snapshot_daily s CROSS JOIN selected_date d
            WHERE s.as_of_date=d.dt
            GROUP BY d.dt
            """,
            {"as_of_date": as_of_date or None},
        ).fetchone() or {}
    strict_count = rows[0].get("strict_candidate_count", 0) if rows else 0
    universe_count = int(coverage.get("universe_count") or 0)
    metric_coverage = {
        "pe_ttm": int(coverage.get("pe_ttm_covered") or 0),
        "pb": int(coverage.get("pb_covered") or 0),
        "forward_pe": int(coverage.get("forward_pe_covered") or 0),
        "forecast_growth": int(coverage.get("forecast_growth_covered") or 0),
        "actual_profit_growth": int(coverage.get("actual_profit_growth_covered") or 0),
        "ret_5d": int(coverage.get("ret_5d_covered") or 0),
        "ret_20d": int(coverage.get("ret_20d_covered") or 0),
    }
    requested_metrics = []
    if pe_filter:
        requested_metrics.append("pe_ttm")
    if pb_lt is not None or pb_gt is not None:
        requested_metrics.append("pb")
    if forward_pe_lt is not None or forward_pe_gt is not None:
        requested_metrics.append("forward_pe")
    if forecast_growth_gte is not None or forecast_growth_lte is not None:
        requested_metrics.append("forecast_growth")
    if actual_growth_gte is not None or actual_growth_lte is not None:
        requested_metrics.append("actual_profit_growth")
    if ret_5d_gte is not None or ret_5d_lte is not None:
        requested_metrics.append("ret_5d")
    if ret_20d_gte is not None or ret_20d_lte is not None:
        requested_metrics.append("ret_20d")
    covered_count = min((metric_coverage[key] for key in requested_metrics), default=universe_count)
    if factor_filters:
        covered_count = min(covered_count, int(factor_coverage.get("factor_scored_assets") or 0))
    warnings = []
    for key in requested_metrics:
        count = metric_coverage[key]
        if universe_count and count / universe_count < 0.8:
            warnings.append(f"{key} coverage is {count}/{universe_count}; strict results are not a complete all-market ranking.")
    if factor_filters and factor_coverage.get("unavailable_factors"):
        warnings.append("Unavailable or disallowed factors: " + ", ".join(factor_coverage["unavailable_factors"]))
    if factor_filters and not factor_coverage.get("factor_score_date"):
        warnings.append("No common point-in-time score date exists for all requested factors.")
    if factor_filters and universe_count and covered_count / universe_count < 0.8:
        warnings.append(f"factor score coverage is {covered_count}/{universe_count}; results are not a complete all-market ranking.")
    emit(
        rows,
        {
            "scope": scope, "theme": theme, "as_of_date": coverage.get("as_of_date") or as_of_date or "latest_on_or_before",
            "sort_by": sort_by, "sort_direction": sort_direction.lower(), "filters": spec,
            "strict_candidate_count": strict_count, "row_count": len(rows), "read_only": True,
            "universe_count": universe_count, "covered_count": covered_count,
            "coverage_pct": round(100 * covered_count / universe_count, 2) if universe_count else 0,
            "metric_coverage": metric_coverage,
            "factor_filters": factor_filters,
            "factor_match": factor_match,
            "factor_lifecycle": factor_lifecycles,
            "factor_coverage": factor_coverage,
            "source_freshness": {
                "technical_date": coverage.get("latest_technical_date"),
                "research_date": coverage.get("latest_research_date"),
                "forecast_date": coverage.get("latest_forecast_date"),
            },
            "warnings": warnings,
            "valuation_scope": "pe_ttm and pb are full-market snapshot factors; forward_pe depends on parsed research forecast coverage",
            "instruction": "Disclose source_dates and factor_score_date. Factor percentiles are same-date cross-sectional ranks, not return probabilities. Distinguish forecast growth from actual_net_profit_yoy.",
        },
    )


def run_research_momentum(root: str, spec: dict[str, Any]) -> None:
    days = clamp_int(spec.get("days"), 30, 1, 365)
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    theme = str(spec.get("theme") or "").strip()
    stock_code = str(spec.get("stock_code") or "").strip()
    terms = theme_terms(theme)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH latest_research AS (
              SELECT max(dt) AS dt FROM analytics.asset_research_daily
            ),
            latest_theme AS (
              SELECT max(as_of_date) AS dt FROM analytics.ai_hardware_theme_scores
            ),
            theme_pool AS (
              SELECT DISTINCT s.stock_code
              FROM analytics.ai_hardware_theme_scores s
              JOIN kg.concepts c ON c.concept_id = s.concept_id
              JOIN latest_theme lt ON s.as_of_date = lt.dt
              WHERE %(has_terms)s = false
                 OR c.name ILIKE ANY(%(patterns)s)
            )
            SELECT r.stock_code, r.stock_name,
                   max(r.dt) AS latest_research_date,
                   sum(r.report_count)::int AS report_count,
                   sum(r.distinct_institution_count)::int AS institution_signal_count,
                   sum(r.buy_count)::int AS buy_count,
                   sum(r.overweight_count)::int AS overweight_count,
                   sum(r.positive_rating_count)::int AS positive_rating_count,
                   sum(r.upgrade_count)::int AS upgrade_count,
                   sum(r.first_coverage_count)::int AS first_coverage_count,
                   sum(r.downgrade_count)::int AS downgrade_count,
                   round(max(r.rating_score_max)::numeric, 2) AS max_rating_score
            FROM analytics.asset_research_daily r
            JOIN latest_research lr ON r.dt BETWEEN lr.dt - (%(days)s::int - 1) AND lr.dt
            WHERE (%(stock_code)s = '' OR r.stock_code = %(stock_code)s)
              AND (%(has_terms)s = false OR r.stock_code IN (SELECT stock_code FROM theme_pool))
            GROUP BY r.stock_code, r.stock_name
            HAVING sum(r.report_count) > 0
            ORDER BY positive_rating_count DESC NULLS LAST,
                     upgrade_count DESC NULLS LAST,
                     first_coverage_count DESC NULLS LAST,
                     report_count DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "days": days,
                "limit": limit,
                "stock_code": stock_code,
                "has_terms": bool(terms),
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
            },
        ).fetchall()
    emit(rows, {"days": days, "theme": theme, "terms": terms, "stock_code": stock_code, "row_count": len(rows), "read_only": True})


def run_screen_stocks(root: str, spec: dict[str, Any]) -> None:
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    days = clamp_int(spec.get("days"), 30, 1, 365)
    theme = str(spec.get("theme") or "").strip()
    stock_code = str(spec.get("stock_code") or "").strip()
    terms = theme_terms(theme)
    pe_lt = as_float(spec.get("pe_lt"))
    pe_gt = as_float(spec.get("pe_gt"))
    pe_filter = pe_lt is not None or pe_gt is not None
    include_negative_pe = bool(spec.get("include_negative_pe", False))
    min_reports = clamp_int(spec.get("min_reports"), 0, 0, 9999)
    min_positive = clamp_int(spec.get("min_positive_ratings"), 0, 0, 9999)
    min_upgrades = clamp_int(spec.get("min_upgrades"), 0, 0, 9999)
    ret_5d_lte = as_float(spec.get("ret_5d_lte"))
    ret_5d_gte = as_float(spec.get("ret_5d_gte"))
    ret_20d_lte = as_float(spec.get("ret_20d_lte"))
    ret_20d_gte = as_float(spec.get("ret_20d_gte"))
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH latest_theme AS (
              SELECT max(as_of_date) AS dt FROM analytics.ai_hardware_theme_scores
            ),
            latest_research AS (
              SELECT max(dt) AS dt FROM analytics.asset_research_daily
            ),
            latest_tech AS (
              SELECT max(dt) AS dt FROM analytics.asset_technical_daily
            ),
            theme_rows AS (
              SELECT s.*, c.name AS theme,
                     row_number() OVER (PARTITION BY s.stock_code ORDER BY s.score DESC NULLS LAST, s.evidence_count_90d DESC NULLS LAST) AS rn
              FROM analytics.ai_hardware_theme_scores s
              JOIN kg.concepts c ON c.concept_id = s.concept_id
              JOIN latest_theme lt ON s.as_of_date = lt.dt
              WHERE (%(stock_code)s = '' OR s.stock_code = %(stock_code)s)
                AND (%(has_terms)s = false OR c.name ILIKE ANY(%(patterns)s))
            ),
            research AS (
              SELECT r.stock_code,
                     sum(r.report_count)::int AS report_count,
                     sum(r.buy_count)::int AS buy_count,
                     sum(r.overweight_count)::int AS overweight_count,
                     sum(r.positive_rating_count)::int AS positive_rating_count,
                     sum(r.upgrade_count)::int AS upgrade_count,
                     sum(r.first_coverage_count)::int AS first_coverage_count,
                     sum(r.downgrade_count)::int AS downgrade_count,
                     max(r.dt) AS latest_research_date
              FROM analytics.asset_research_daily r
              JOIN latest_research lr ON r.dt BETWEEN lr.dt - (%(days)s::int - 1) AND lr.dt
              GROUP BY r.stock_code
            ),
            tech AS (
              SELECT a.symbol AS stock_code, t.dt AS technical_date, t.close, t.pct_chg_1d, t.ret_5d, t.ret_20d, t.pattern_tags
              FROM analytics.asset_technical_daily t
              JOIN latest_tech lt ON t.dt = lt.dt
              JOIN market.assets a ON a.asset_id = t.asset_id
              WHERE a.asset_type = 'stock'
            )
            SELECT tr.stock_code, tr.stock_name, tr.theme, tr.relation_type,
                   round(tr.score::numeric, 2) AS theme_score,
                   tr.forward_pe,
                   round(tr.forecast_eps_growth::numeric, 4) AS forecast_growth,
                   tr.mention_count_30d, tr.evidence_count_90d, tr.research_report_count_180d,
                   tr.latest_rating, tr.latest_rating_change,
                   coalesce(r.report_count, 0) AS recent_report_count,
                   coalesce(r.buy_count, 0) AS buy_count,
                   coalesce(r.overweight_count, 0) AS overweight_count,
                   coalesce(r.positive_rating_count, 0) AS positive_rating_count,
                   coalesce(r.upgrade_count, 0) AS upgrade_count,
                   coalesce(r.first_coverage_count, 0) AS first_coverage_count,
                   coalesce(r.downgrade_count, 0) AS downgrade_count,
                   r.latest_research_date,
                   tech.technical_date,
                   round(tech.ret_5d::numeric, 4) AS ret_5d,
                   round(tech.ret_20d::numeric, 4) AS ret_20d,
                   tech.pattern_tags,
                   left((tr.evidence->'evidence'->>0), 280) AS evidence
            FROM theme_rows tr
            LEFT JOIN research r ON r.stock_code = tr.stock_code
            LEFT JOIN tech ON tech.stock_code = tr.stock_code
            WHERE tr.rn = 1
              AND (%(pe_lt)s::float8 IS NULL OR tr.forward_pe < %(pe_lt)s)
              AND (%(pe_gt)s::float8 IS NULL OR tr.forward_pe > %(pe_gt)s)
              AND (%(pe_filter)s = false OR %(include_negative_pe)s = true OR tr.forward_pe > 0)
              AND coalesce(r.report_count, 0) >= %(min_reports)s
              AND coalesce(r.positive_rating_count, 0) >= %(min_positive)s
              AND coalesce(r.upgrade_count, 0) >= %(min_upgrades)s
              AND (%(ret_5d_lte)s::float8 IS NULL OR tech.ret_5d <= %(ret_5d_lte)s)
              AND (%(ret_5d_gte)s::float8 IS NULL OR tech.ret_5d >= %(ret_5d_gte)s)
              AND (%(ret_20d_lte)s::float8 IS NULL OR tech.ret_20d <= %(ret_20d_lte)s)
              AND (%(ret_20d_gte)s::float8 IS NULL OR tech.ret_20d >= %(ret_20d_gte)s)
            ORDER BY positive_rating_count DESC NULLS LAST,
                     upgrade_count DESC NULLS LAST,
                     recent_report_count DESC NULLS LAST,
                     theme_score DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "stock_code": stock_code,
                "has_terms": bool(terms),
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
                "days": days,
                "pe_lt": pe_lt,
                "pe_gt": pe_gt,
                "pe_filter": pe_filter,
                "include_negative_pe": include_negative_pe,
                "min_reports": min_reports,
                "min_positive": min_positive,
                "min_upgrades": min_upgrades,
                "ret_5d_lte": ret_5d_lte,
                "ret_5d_gte": ret_5d_gte,
                "ret_20d_lte": ret_20d_lte,
                "ret_20d_gte": ret_20d_gte,
                "limit": limit,
            },
        ).fetchall()
    emit(rows, {"days": days, "theme": theme, "terms": terms, "filters": spec, "row_count": len(rows), "read_only": True})


def run_stock_profile(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    days = clamp_int(spec.get("days"), 30, 1, 365)
    limit = clamp_int(spec.get("limit"), 80, 1, 200)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH latest_theme AS (
              SELECT max(as_of_date) AS dt FROM analytics.ai_hardware_theme_scores
            ),
            latest_tech AS (
              SELECT max(dt) AS dt FROM analytics.asset_technical_daily
            ),
            target AS (
              SELECT DISTINCT stock_code, stock_name
              FROM analytics.ai_hardware_theme_scores
              WHERE (%(stock_code)s <> '' AND stock_code = %(stock_code)s)
                 OR (%(stock_name)s <> '' AND stock_name ILIKE '%%' || %(stock_name)s || '%%')
              LIMIT 1
            ),
            themes AS (
              SELECT s.stock_code, s.stock_name, c.name AS theme, s.relation_type,
                     round(s.score::numeric, 2) AS score, s.forward_pe,
                     round(s.forecast_eps_growth::numeric, 4) AS forecast_growth,
                     s.latest_rating, s.latest_rating_change,
                     s.mention_count_30d, s.evidence_count_90d, s.research_report_count_180d,
                     left((s.evidence->'evidence'->>0), 260) AS evidence
              FROM analytics.ai_hardware_theme_scores s
              JOIN kg.concepts c ON c.concept_id = s.concept_id
              JOIN latest_theme lt ON s.as_of_date = lt.dt
              JOIN target t ON t.stock_code = s.stock_code
              ORDER BY s.score DESC NULLS LAST
              LIMIT 12
            ),
            research AS (
              SELECT report_date, institution, rating, rating_change, report_title
              FROM research.reports_resolved r
              JOIN target t ON t.stock_code = r.stock_code
              WHERE r.report_date >= (SELECT max(report_date) FROM research.reports_resolved) - (%(days)s::int - 1)
              ORDER BY report_date DESC, institution
              LIMIT 20
            ),
            tech AS (
              SELECT a.symbol AS stock_code, a.name AS stock_name, t.dt, t.close, t.pct_chg_1d, t.ret_5d, t.ret_20d,
                     t.new_high_20d, t.new_low_20d, t.breakout_20d, t.local_peak, t.local_trough, t.pattern_tags
              FROM analytics.asset_technical_daily t
              JOIN latest_tech lt ON t.dt = lt.dt
              JOIN market.assets a ON a.asset_id = t.asset_id
              JOIN target tar ON tar.stock_code = a.symbol
            ),
            events AS (
              SELECT event_date, event_type, polarity, round(impact_score::numeric, 2) AS impact,
                     confidence, industries, keywords, summary, left(evidence, 260) AS evidence
              FROM analytics.document_events e
              JOIN target t ON t.stock_code = ANY(e.stock_codes)
              WHERE e.event_date >= (SELECT max(event_date) FROM analytics.document_events) - (%(days)s::int - 1)
              ORDER BY impact_score DESC NULLS LAST, event_date DESC
              LIMIT 20
            )
            SELECT 'theme' AS section, row_to_json(themes.*) AS data FROM themes
            UNION ALL
            SELECT 'technical' AS section, row_to_json(tech.*) AS data FROM tech
            UNION ALL
            SELECT 'research' AS section, row_to_json(research.*) AS data FROM research
            UNION ALL
            SELECT 'event' AS section, row_to_json(events.*) AS data FROM events
            LIMIT %(limit)s
            """,
            {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "days": days,
                "limit": limit,
            },
        ).fetchall()
    emit(rows, {"stock_code": stock_code, "stock_name": stock_name, "days": days, "row_count": len(rows), "read_only": True})


# Meilisearch is intentionally a bounded recent cache. Auto-routing a longer
# window there would return partial results whenever the cache has any hit.
MEILI_AUTO_MAX_DAYS = 2


def run_document_search(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("query") or "").strip()
    original_terms = query_terms(query, spec.get("keywords"))
    terms, expansion_matches = expand_terms_from_catalog(root, original_terms)
    expansion_meta = {
        "original_terms": original_terms,
        "catalog_expansion_count": len(expansion_matches),
        "catalog_expansion": expansion_matches[:8],
    }
    source_types = source_types_arg(spec.get("source_types"))
    days = clamp_int(spec.get("days"), 30, 1, 3650)
    limit = clamp_int(spec.get("limit"), 30, 1, 100)
    stock_code = str(spec.get("stock_code") or "").strip()
    require_all = bool(spec.get("require_all_terms", False))
    include_body = bool(spec.get("include_body", False))
    backend = normalize_search_backend(spec.get("backend"), {"auto", "meilisearch", "postgres_fts", "legacy"})
    if (
        terms
        and not require_all
        and backend in {"auto", "meilisearch"}
        and (backend == "meilisearch" or days <= MEILI_AUTO_MAX_DAYS)
    ):
        meili_rows, meili_meta = meili_document_rows(
            root,
            query,
            terms,
            source_types or ["news", "peoples_daily", "research_report"],
            days,
            stock_code,
            limit,
            clamp_int(spec.get("snippet_chars"), 420, 120, 1200),
            bm25=False,
        )
        if meili_rows:
            emit(
                meili_rows,
                {
                    "query": query,
                    "terms": terms,
                    "source_types": source_types,
                    "days": days,
                    "include_body": include_body,
                    "row_count": len(meili_rows),
                    "search_backend": "meilisearch",
                    "read_only": True,
                    **expansion_meta,
                    **meili_meta,
                },
            )
            return
        if backend == "meilisearch":
            emit(
                [],
                {
                    "query": query,
                    "terms": terms,
                    "source_types": source_types,
                    "days": days,
                    "include_body": include_body,
                    "row_count": 0,
                    "search_backend": "meilisearch",
                    "read_only": True,
                    **expansion_meta,
                    **meili_meta,
                },
            )
            return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        if terms and backend in {"auto", "postgres_fts"}:
            fts_rows, fts_meta = fts_document_rows(
                conn,
                query,
                terms,
                source_types or ["news", "peoples_daily", "research_report"],
                days,
                stock_code,
                limit,
                clamp_int(spec.get("snippet_chars"), 420, 120, 1200),
                bm25=False,
            )
            if fts_rows:
                emit(
                    fts_rows,
                    {
                        "query": query,
                        "terms": terms,
                        "source_types": source_types,
                        "days": days,
                        "include_body": include_body,
                        "row_count": len(fts_rows),
                        "search_backend": "postgres_fts",
                        "read_only": True,
                        **expansion_meta,
                        **fts_meta,
                    },
                )
                return
            if backend == "postgres_fts":
                emit(
                    [],
                    {
                        "query": query,
                        "terms": terms,
                        "source_types": source_types,
                        "days": days,
                        "include_body": include_body,
                        "row_count": 0,
                        "search_backend": "postgres_fts",
                        "read_only": True,
                        **expansion_meta,
                        **fts_meta,
                    },
                )
                return
        if backend not in {"auto", "legacy"}:
            emit(
                [],
                {
                    "query": query,
                    "terms": terms,
                    "source_types": source_types,
                    "days": days,
                    "include_body": include_body,
                    "row_count": 0,
                    "search_backend": backend,
                    "read_only": True,
                    **expansion_meta,
                    "note": "requested backend is not supported for finance_document_search fallback",
                },
            )
            return
        rows = conn.execute(
            """
            WITH bounds AS (
              SELECT max(published_at) AS latest_ts FROM corpus.documents
            ),
            recent_docs AS (
              SELECT d.id, d.source_type, d.source_name, d.published_at, d.title, d.url,
                     coalesce(d.content_text, d.content_md, d.raw_text, '') AS body
              FROM corpus.documents d
              CROSS JOIN bounds b
              WHERE d.published_at >= b.latest_ts - ((%(days)s::int - 1) * interval '1 day')
                AND (%(has_sources)s = false OR d.source_type = ANY(%(source_types)s))
            ),
            candidate_ids AS (
              SELECT id AS document_id, 8::numeric AS candidate_score
              FROM recent_docs
              WHERE %(has_terms)s = false OR title ILIKE ANY(%(patterns)s)
              UNION ALL
              SELECT k.document_id, sum(k.weight) AS candidate_score
              FROM corpus.document_keywords k
              JOIN recent_docs d ON d.id = k.document_id
              WHERE %(has_terms)s = true AND k.keyword ILIKE ANY(%(patterns)s)
              GROUP BY k.document_id
              UNION ALL
              SELECT e.document_id, (max(coalesce(e.impact_score, 0)) + max(coalesce(e.confidence, 0)))::numeric AS candidate_score
              FROM analytics.document_events e
              JOIN recent_docs d ON d.id = e.document_id
              WHERE %(has_terms)s = true
                AND (
                  e.summary ILIKE ANY(%(patterns)s)
                  OR e.evidence ILIKE ANY(%(patterns)s)
                  OR array_to_string(e.keywords, ',') ILIKE ANY(%(patterns)s)
                  OR array_to_string(e.industries, ',') ILIKE ANY(%(patterns)s)
                )
              GROUP BY e.document_id
              UNION ALL
              SELECT id AS document_id, 3::numeric AS candidate_score
              FROM recent_docs
              WHERE %(include_body)s = true
                AND %(has_terms)s = true
                AND body ILIKE ANY(%(patterns)s)
            ),
            kw AS (
              SELECT document_id,
                     array_agg(keyword ORDER BY weight DESC, keyword) FILTER (WHERE keyword ILIKE ANY(%(patterns)s)) AS matched_keywords,
                     sum(weight) FILTER (WHERE keyword ILIKE ANY(%(patterns)s)) AS keyword_score
              FROM corpus.document_keywords
              GROUP BY document_id
            ),
            candidates AS (
              SELECT document_id, sum(candidate_score) AS candidate_score
              FROM candidate_ids
              GROUP BY document_id
            ),
            base AS (
              SELECT d.id, d.source_type, d.source_name, d.published_at::date AS published_date,
                     d.title, d.url,
                     d.body,
                     coalesce(kw.matched_keywords, ARRAY[]::text[]) AS matched_keywords,
                     coalesce(kw.keyword_score, 0) AS keyword_score,
                     coalesce(c.candidate_score, 0) AS candidate_score,
                     r.stock_code, r.stock_name, r.institution, r.rating, r.rating_change, r.industry_name
              FROM recent_docs d
              JOIN candidates c ON c.document_id = d.id
              LEFT JOIN kw ON kw.document_id = d.id
              LEFT JOIN research.reports_resolved r ON r.document_id = d.id
              WHERE (%(stock_code)s = '' OR r.stock_code = %(stock_code)s)
            ),
            scored AS (
              SELECT *,
                     (
                       CASE WHEN %(has_terms)s = false THEN 1 ELSE 0 END
                       + CASE WHEN title ILIKE ANY(%(patterns)s) THEN 8 ELSE 0 END
                       + CASE WHEN %(include_body)s = true AND body ILIKE ANY(%(patterns)s) THEN 3 ELSE 0 END
                       + keyword_score
                       + candidate_score
                     ) AS score,
                     CASE
                       WHEN %(has_terms)s = false THEN true
                       WHEN %(require_all)s = true THEN (%(include_body)s = true AND body ILIKE ALL(%(patterns)s)) OR title ILIKE ALL(%(patterns)s)
                       ELSE true
                     END AS is_match
              FROM base
            )
            SELECT id AS document_id, source_type, source_name, published_date, title,
                   stock_code, stock_name, institution, rating, rating_change, industry_name,
                   round(score::numeric, 3) AS score,
                   matched_keywords,
                   left(body, %(snippet_chars)s) AS snippet,
                   url
            FROM scored
            WHERE is_match
            ORDER BY score DESC NULLS LAST, published_date DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
                "has_terms": bool(terms),
                "require_all": require_all,
                "days": days,
                "limit": limit,
                "snippet_chars": clamp_int(spec.get("snippet_chars"), 420, 120, 1200),
                "has_sources": bool(source_types),
                "source_types": source_types or ["news", "peoples_daily", "research_report"],
                "stock_code": stock_code,
                "include_body": include_body,
            },
        ).fetchall()
    emit(rows, {"query": query, "terms": terms, "source_types": source_types, "days": days, "include_body": include_body, "row_count": len(rows), "search_backend": "legacy", "read_only": True, **expansion_meta})


def run_document_bm25_search(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("query") or "").strip()
    original_terms = bm25_query_terms(query, spec.get("keywords"))
    terms, expansion_matches = expand_terms_from_catalog(root, original_terms)
    expansion_meta = {
        "original_terms": original_terms,
        "catalog_expansion_count": len(expansion_matches),
        "catalog_expansion": expansion_matches[:8],
    }
    source_types = source_types_arg(spec.get("source_types"))
    days = clamp_int(spec.get("days"), 30, 1, 3650)
    limit = clamp_int(spec.get("limit"), 30, 1, 100)
    candidate_limit = clamp_int(spec.get("candidate_limit"), 500, 50, 2500)
    snippet_chars = clamp_int(spec.get("snippet_chars"), 420, 120, 1200)
    stock_code = str(spec.get("stock_code") or "").strip()
    include_body_candidates = bool(spec.get("include_body_candidates", False))
    backend = normalize_search_backend(spec.get("backend"), {"auto", "hybrid", "meilisearch", "postgres_fts", "local_bm25"})

    if not terms:
        emit([], {"query": query, "terms": terms, "row_count": 0, "read_only": True, "note": "empty query", **expansion_meta})
        return

    if backend in {"auto", "hybrid"}:
        hybrid_rows, hybrid_meta = hybrid_document_rows(
            root, query, terms,
            source_types or ["news", "peoples_daily", "research_report"],
            days, stock_code, limit, snippet_chars,
        )
        if hybrid_rows:
            emit(
                hybrid_rows,
                {
                    "query": query, "terms": terms, "source_types": source_types,
                    "days": days, "candidate_limit": candidate_limit,
                    "candidate_count": hybrid_meta.get("hybrid_candidates", len(hybrid_rows)),
                    "include_body_candidates": include_body_candidates,
                    "row_count": len(hybrid_rows), "search_backend": "hybrid",
                    "read_only": True, **expansion_meta, **hybrid_meta,
                },
            )
            return
        if backend == "hybrid":
            emit([], {
                "query": query, "terms": terms, "source_types": source_types,
                "days": days, "row_count": 0, "search_backend": "hybrid",
                "read_only": True, **expansion_meta, **hybrid_meta,
            })
            return

    if backend in {"auto", "meilisearch"} and (backend == "meilisearch" or days <= MEILI_AUTO_MAX_DAYS):
        meili_rows, meili_meta = meili_document_rows(
            root,
            query,
            terms,
            source_types or ["news", "peoples_daily", "research_report"],
            days,
            stock_code,
            limit,
            snippet_chars,
            bm25=True,
        )
        if meili_rows:
            emit(
                meili_rows,
                {
                    "query": query,
                    "terms": terms,
                    "source_types": source_types,
                    "days": days,
                    "candidate_limit": candidate_limit,
                    "candidate_count": len(meili_rows),
                    "include_body_candidates": include_body_candidates,
                    "row_count": len(meili_rows),
                    "search_backend": "meilisearch",
                    "read_only": True,
                    **expansion_meta,
                    **meili_meta,
                },
            )
            return
        if backend == "meilisearch":
            emit(
                [],
                {
                    "query": query,
                    "terms": terms,
                    "source_types": source_types,
                    "days": days,
                    "candidate_limit": candidate_limit,
                    "candidate_count": 0,
                    "include_body_candidates": include_body_candidates,
                    "row_count": 0,
                    "search_backend": "meilisearch",
                    "read_only": True,
                    **expansion_meta,
                    **meili_meta,
                },
            )
            return

    conn = connect(root)
    with conn:
        conn.execute("SET TRANSACTION READ ONLY")
        if backend in {"auto", "postgres_fts"}:
            fts_rows, fts_meta = fts_document_rows(
                conn,
                query,
                terms,
                source_types or ["news", "peoples_daily", "research_report"],
                days,
                stock_code,
                limit,
                snippet_chars,
                bm25=True,
            )
            if fts_rows:
                emit(
                    fts_rows,
                    {
                        "query": query,
                        "terms": terms,
                        "source_types": source_types,
                        "days": days,
                        "candidate_limit": candidate_limit,
                        "candidate_count": len(fts_rows),
                        "include_body_candidates": include_body_candidates,
                        "row_count": len(fts_rows),
                        "search_backend": "postgres_fts",
                        "read_only": True,
                        **expansion_meta,
                        **fts_meta,
                    },
                )
                return
            if backend == "postgres_fts":
                emit(
                    [],
                    {
                        "query": query,
                        "terms": terms,
                        "source_types": source_types,
                        "days": days,
                        "candidate_limit": candidate_limit,
                        "candidate_count": 0,
                        "include_body_candidates": include_body_candidates,
                        "row_count": 0,
                        "search_backend": "postgres_fts",
                        "read_only": True,
                        **expansion_meta,
                        **fts_meta,
                    },
                )
                return
        rows = conn.execute(
            """
            WITH bounds AS (
              SELECT max(published_at) AS latest_ts FROM corpus.documents
            ),
            recent_docs AS (
              SELECT d.id, d.source_type, d.source_name, d.published_at, d.title, d.url
              FROM corpus.documents d
              CROSS JOIN bounds b
              WHERE d.published_at >= b.latest_ts - ((%(days)s::int - 1) * interval '1 day')
                AND (%(has_sources)s = false OR d.source_type = ANY(%(source_types)s))
            ),
            candidate_ids AS (
              SELECT id AS document_id, 10::numeric AS candidate_score
              FROM recent_docs
              WHERE title ILIKE ANY(%(patterns)s)
              UNION ALL
              SELECT k.document_id, sum(k.weight) AS candidate_score
              FROM corpus.document_keywords k
              JOIN recent_docs d ON d.id = k.document_id
              WHERE k.keyword ILIKE ANY(%(patterns)s)
              GROUP BY k.document_id
              UNION ALL
              SELECT e.document_id,
                     (max(coalesce(e.impact_score, 0)) + max(coalesce(e.confidence, 0)) + 2)::numeric AS candidate_score
              FROM analytics.document_events e
              JOIN recent_docs d ON d.id = e.document_id
              WHERE e.summary ILIKE ANY(%(patterns)s)
                 OR e.evidence ILIKE ANY(%(patterns)s)
                 OR array_to_string(e.keywords, ',') ILIKE ANY(%(patterns)s)
                 OR array_to_string(e.industries, ',') ILIKE ANY(%(patterns)s)
              GROUP BY e.document_id
              UNION ALL
              SELECT d.id AS document_id, 2::numeric AS candidate_score
              FROM corpus.documents d
              CROSS JOIN bounds b
              WHERE %(include_body_candidates)s = true
                AND d.published_at >= b.latest_ts - ((%(days)s::int - 1) * interval '1 day')
                AND (%(has_sources)s = false OR d.source_type = ANY(%(source_types)s))
                AND coalesce(d.content_text, d.content_md, d.raw_text, '') ILIKE ANY(%(patterns)s)
            ),
            candidates AS (
              SELECT c.document_id, sum(c.candidate_score) AS candidate_score, max(d.published_at) AS published_at
              FROM candidate_ids c
              JOIN recent_docs d ON d.id = c.document_id
              GROUP BY c.document_id
              ORDER BY sum(c.candidate_score) DESC NULLS LAST, max(d.published_at) DESC NULLS LAST
              LIMIT %(candidate_limit)s
            ),
            kw AS (
              SELECT document_id,
                     array_agg(keyword ORDER BY weight DESC, keyword) AS keywords,
                     array_agg(keyword ORDER BY weight DESC, keyword) FILTER (WHERE keyword ILIKE ANY(%(patterns)s)) AS matched_keywords
              FROM corpus.document_keywords
              WHERE document_id IN (SELECT document_id FROM candidates)
              GROUP BY document_id
            )
            SELECT d.id, d.source_type, d.source_name, d.published_at::date AS published_date,
                   d.title, d.url,
                   coalesce(d.content_text, d.content_md, d.raw_text, '') AS body,
                   coalesce(kw.keywords, ARRAY[]::text[]) AS keywords,
                   coalesce(kw.matched_keywords, ARRAY[]::text[]) AS matched_keywords,
                   round(c.candidate_score::numeric, 3) AS candidate_score,
                   r.stock_code, r.stock_name, r.institution, r.rating, r.rating_change, r.industry_name
            FROM candidates c
            JOIN corpus.documents d ON d.id = c.document_id
            LEFT JOIN kw ON kw.document_id = d.id
            LEFT JOIN LATERAL (
              SELECT stock_code, stock_name, institution, rating, rating_change, industry_name
              FROM research.reports_resolved r
              WHERE r.document_id = d.id
                AND (%(stock_code)s = '' OR r.stock_code = %(stock_code)s)
              ORDER BY r.report_date DESC NULLS LAST
              LIMIT 1
            ) r ON true
            WHERE %(stock_code)s = '' OR r.stock_code = %(stock_code)s
            ORDER BY c.candidate_score DESC NULLS LAST, d.published_at DESC NULLS LAST
            """,
            {
                "patterns": [f"%{term}%" for term in terms],
                "days": days,
                "candidate_limit": candidate_limit,
                "has_sources": bool(source_types),
                "source_types": source_types or ["news", "peoples_daily", "research_report"],
                "stock_code": stock_code,
                "include_body_candidates": include_body_candidates,
            },
        ).fetchall()

    scored_rows = score_bm25_rows(rows, terms, limit, snippet_chars)
    emit(
        scored_rows,
        {
            "query": query,
            "terms": terms,
            "source_types": source_types,
            "days": days,
            "candidate_limit": candidate_limit,
            "candidate_count": len(rows),
            "include_body_candidates": include_body_candidates,
            "row_count": len(scored_rows),
            "search_backend": "local_bm25",
            "read_only": True,
            **expansion_meta,
        },
    )


def run_topic_clusters(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("query") or spec.get("theme") or "").strip()
    terms = query_terms(query, spec.get("keywords"))
    source_types = source_types_arg(spec.get("source_types"))
    days = clamp_int(spec.get("days"), 30, 1, 3650)
    limit = clamp_int(spec.get("limit"), 30, 1, 100)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH bounds AS (
              SELECT max(published_at) AS latest_ts FROM corpus.documents
            ),
            matched_docs AS (
              SELECT d.id, d.source_type, d.source_name, d.published_at::date AS published_date, d.title,
                     coalesce(d.content_text, d.content_md, d.raw_text, '') AS body
              FROM corpus.documents d
              CROSS JOIN bounds b
              WHERE d.published_at >= b.latest_ts - ((%(days)s::int - 1) * interval '1 day')
                AND (%(has_sources)s = false OR d.source_type = ANY(%(source_types)s))
                AND (
                  %(has_terms)s = false
                  OR d.title ILIKE ANY(%(patterns)s)
                  OR EXISTS (
                    SELECT 1 FROM corpus.document_keywords k
                    WHERE k.document_id = d.id AND k.keyword ILIKE ANY(%(patterns)s)
                  )
                  OR EXISTS (
                    SELECT 1 FROM analytics.document_events e
                    WHERE e.document_id = d.id
                      AND (
                        e.summary ILIKE ANY(%(patterns)s)
                        OR e.evidence ILIKE ANY(%(patterns)s)
                        OR array_to_string(e.keywords, ',') ILIKE ANY(%(patterns)s)
                        OR array_to_string(e.industries, ',') ILIKE ANY(%(patterns)s)
                      )
                  )
                )
            ),
            keyword_clusters AS (
              SELECT 'keyword' AS cluster_type,
                     k.keyword AS cluster,
                     count(DISTINCT m.id) AS doc_count,
                     max(m.published_date) AS latest_date,
                     round(avg(k.weight)::numeric, 3) AS avg_weight,
                     array_agg(DISTINCT m.source_type) AS source_types,
                     (array_agg(DISTINCT m.title))[1:6] AS sample_titles
              FROM matched_docs m
              JOIN corpus.document_keywords k ON k.document_id = m.id
              WHERE k.keyword <> ALL(%(stop_keywords)s)
              GROUP BY k.keyword
            ),
            event_clusters AS (
              SELECT 'event' AS cluster_type,
                     e.event_type AS cluster,
                     count(DISTINCT m.id) AS doc_count,
                     max(e.event_date) AS latest_date,
                     round(avg(e.impact_score)::numeric, 3) AS avg_weight,
                     array_agg(DISTINCT m.source_type) AS source_types,
                     (array_agg(DISTINCT e.summary))[1:6] AS sample_titles
              FROM matched_docs m
              JOIN analytics.document_events e ON e.document_id = m.id
              GROUP BY e.event_type
            )
            SELECT * FROM (
              SELECT * FROM keyword_clusters
              UNION ALL
              SELECT * FROM event_clusters
            ) x
            ORDER BY doc_count DESC, avg_weight DESC NULLS LAST, latest_date DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
                "has_terms": bool(terms),
                "days": days,
                "limit": limit,
                "has_sources": bool(source_types),
                "source_types": source_types or ["news", "peoples_daily", "research_report"],
                "stop_keywords": list(CLUSTER_STOP_KEYWORDS),
            },
        ).fetchall()
    emit(rows, {"query": query, "terms": terms, "source_types": source_types, "days": days, "row_count": len(rows), "read_only": True})


def run_document_evidence(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("query") or spec.get("theme") or "").strip()
    terms = query_terms(query, spec.get("keywords"))
    source_types = source_types_arg(spec.get("source_types"))
    days = clamp_int(spec.get("days"), 30, 1, 3650)
    limit = clamp_int(spec.get("limit"), 30, 1, 100)
    candidate_limit = clamp_int(spec.get("candidate_limit"), 500, 50, 2500)
    include_body = bool(spec.get("include_body", False))
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH bounds AS (
              SELECT max(published_at) AS latest_ts FROM corpus.documents
            ),
            recent_docs AS (
              SELECT d.id, d.source_type, d.source_name, d.published_at, d.title, d.url
              FROM corpus.documents d
              CROSS JOIN bounds b
              WHERE d.published_at >= b.latest_ts - ((%(days)s::int - 1) * interval '1 day')
                AND (%(has_sources)s = false OR d.source_type = ANY(%(source_types)s))
            ),
            candidate_ids AS (
              SELECT id AS document_id, 8::numeric AS candidate_score
              FROM recent_docs
              WHERE %(has_terms)s = false OR title ILIKE ANY(%(patterns)s)
              UNION ALL
              SELECT k.document_id, sum(k.weight) AS candidate_score
              FROM corpus.document_keywords k
              JOIN recent_docs d ON d.id = k.document_id
              WHERE %(has_terms)s = true
                AND k.keyword ILIKE ANY(%(patterns)s)
              GROUP BY k.document_id
              UNION ALL
              SELECT e.document_id,
                     (max(coalesce(e.impact_score, 0)) + max(coalesce(e.confidence, 0)) + 2)::numeric AS candidate_score
              FROM analytics.document_events e
              JOIN recent_docs d ON d.id = e.document_id
              WHERE %(has_terms)s = true
                AND (
                  e.evidence ILIKE ANY(%(patterns)s)
                  OR e.summary ILIKE ANY(%(patterns)s)
                  OR array_to_string(e.keywords, ',') ILIKE ANY(%(patterns)s)
                  OR array_to_string(e.industries, ',') ILIKE ANY(%(patterns)s)
                )
              GROUP BY e.document_id
              UNION ALL
              SELECT d.id AS document_id, 2::numeric AS candidate_score
              FROM corpus.documents d
              CROSS JOIN bounds b
              WHERE %(include_body)s = true
                AND %(has_terms)s = true
                AND d.published_at >= b.latest_ts - ((%(days)s::int - 1) * interval '1 day')
                AND (%(has_sources)s = false OR d.source_type = ANY(%(source_types)s))
                AND coalesce(d.content_text, d.content_md, d.raw_text, '') ILIKE ANY(%(patterns)s)
            ),
            candidates AS (
              SELECT c.document_id, sum(c.candidate_score) AS candidate_score, max(d.published_at) AS published_at
              FROM candidate_ids c
              JOIN recent_docs d ON d.id = c.document_id
              GROUP BY c.document_id
              ORDER BY sum(c.candidate_score) DESC NULLS LAST, max(d.published_at) DESC NULLS LAST
              LIMIT %(candidate_limit)s
            ),
            docs AS (
              SELECT d.id, d.source_type, d.source_name, d.published_at::date AS published_date,
                     d.title, d.url, coalesce(d.content_text, d.content_md, d.raw_text, '') AS body,
                     c.candidate_score
              FROM candidates c
              JOIN corpus.documents d ON d.id = c.document_id
            ),
            doc_hits AS (
              SELECT id AS document_id, source_type, source_name, published_date, title,
                     null::text AS event_type, null::smallint AS polarity,
                     null::numeric AS impact,
                     left(body, %(snippet_chars)s) AS evidence,
                     url,
                     CASE WHEN title ILIKE ANY(%(patterns)s) THEN 8 ELSE 0 END
                     + candidate_score
                     + CASE WHEN %(include_body)s = true AND body ILIKE ANY(%(patterns)s) THEN 3 ELSE 0 END AS score
              FROM docs
            ),
            event_hits AS (
              SELECT d.id AS document_id, d.source_type, d.source_name, d.published_date, d.title,
                     e.event_type, e.polarity, round(e.impact_score::numeric, 2) AS impact,
                     left(e.evidence, %(snippet_chars)s) AS evidence,
                     d.url,
                     coalesce(e.impact_score, 0) + coalesce(e.confidence, 0) AS score
              FROM docs d
              JOIN analytics.document_events e ON e.document_id = d.id
              WHERE %(has_terms)s = false
                 OR e.evidence ILIKE ANY(%(patterns)s)
                 OR e.summary ILIKE ANY(%(patterns)s)
                 OR array_to_string(e.keywords, ',') ILIKE ANY(%(patterns)s)
                 OR array_to_string(e.industries, ',') ILIKE ANY(%(patterns)s)
            )
            SELECT * FROM (
              SELECT * FROM event_hits
              UNION ALL
              SELECT * FROM doc_hits
            ) x
            WHERE evidence IS NOT NULL AND evidence <> ''
            ORDER BY score DESC NULLS LAST, published_date DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
                "has_terms": bool(terms),
                "days": days,
                "limit": limit,
                "snippet_chars": clamp_int(spec.get("snippet_chars"), 520, 120, 1500),
                "has_sources": bool(source_types),
                "source_types": source_types or ["news", "peoples_daily", "research_report"],
                "candidate_limit": candidate_limit,
                "include_body": include_body,
            },
        ).fetchall()
    emit(rows, {"query": query, "terms": terms, "source_types": source_types, "days": days, "candidate_limit": candidate_limit, "include_body": include_body, "row_count": len(rows), "read_only": True})


def run_subindustry_trends(root: str, spec: dict[str, Any]) -> None:
    subindustry = str(spec.get("subindustry") or spec.get("query") or "").strip()
    terms = query_terms(subindustry)
    days = clamp_int(spec.get("days"), 30, 1, 365)
    limit = clamp_int(spec.get("limit"), 30, 1, 100)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH latest AS (
              SELECT max(dt) AS dt FROM analytics.ai_hardware_subindustry_daily
            ),
            current_window AS (
              SELECT s.*
              FROM analytics.ai_hardware_subindustry_daily s
              JOIN latest l ON s.dt BETWEEN l.dt - (%(days)s::int - 1) AND l.dt
              WHERE %(has_terms)s = false OR s.subindustry ILIKE ANY(%(patterns)s)
            ),
            previous_window AS (
              SELECT s.*
              FROM analytics.ai_hardware_subindustry_daily s
              JOIN latest l ON s.dt BETWEEN l.dt - (%(days)s::int * 2 - 1) AND l.dt - %(days)s::int
              WHERE %(has_terms)s = false OR s.subindustry ILIKE ANY(%(patterns)s)
            ),
            cur AS (
              SELECT subindustry,
                     sum(mention_count)::int AS mention_count,
                     sum(distinct_document_count)::int AS document_count,
                     max(distinct_stock_count)::int AS distinct_stock_count,
                     sum(positive_event_score) AS positive_event_score,
                     sum(negative_event_score) AS negative_event_score,
                     sum(research_report_count)::int AS research_report_count,
                     sum(positive_rating_count)::int AS positive_rating_count,
                     round(avg(avg_forward_pe)::numeric, 2) AS avg_forward_pe,
                     round(avg(avg_forecast_eps_growth)::numeric, 4) AS avg_forecast_eps_growth
              FROM current_window
              GROUP BY subindustry
            ),
            prev AS (
              SELECT subindustry,
                     sum(mention_count)::int AS mention_count,
                     sum(distinct_document_count)::int AS document_count,
                     sum(research_report_count)::int AS research_report_count
              FROM previous_window
              GROUP BY subindustry
            ),
            latest_rows AS (
              SELECT DISTINCT ON (s.subindustry)
                     s.subindustry, s.dt AS latest_date, s.top_terms, s.top_stocks, s.evidence
              FROM analytics.ai_hardware_subindustry_daily s
              JOIN latest l ON s.dt <= l.dt
              WHERE %(has_terms)s = false OR s.subindustry ILIKE ANY(%(patterns)s)
              ORDER BY s.subindustry, s.dt DESC
            )
            SELECT cur.subindustry,
                   lr.latest_date, ((now() AT TIME ZONE 'Asia/Shanghai')::date-lr.latest_date) AS data_age_days,
                   cur.mention_count,
                   coalesce(prev.mention_count, 0) AS previous_mention_count,
                   (cur.mention_count - coalesce(prev.mention_count, 0))::int AS mention_delta,
                   CASE WHEN coalesce(prev.mention_count, 0) > 0
                        THEN round(((cur.mention_count - prev.mention_count)::numeric / prev.mention_count), 4)
                        ELSE NULL END AS mention_growth,
                   cur.document_count,
                   cur.distinct_stock_count,
                   round(cur.positive_event_score::numeric, 3) AS positive_event_score,
                   round(cur.negative_event_score::numeric, 3) AS negative_event_score,
                   cur.research_report_count,
                   coalesce(prev.research_report_count, 0) AS previous_research_report_count,
                   cur.positive_rating_count,
                   cur.avg_forward_pe,
                   cur.avg_forecast_eps_growth,
                   lr.top_terms,
                   (SELECT jsonb_agg(x-ARRAY['score','forward_pe','forecast_growth'])
                    FROM jsonb_array_elements(coalesce(lr.top_stocks,'[]'::jsonb)) x) AS top_stocks,
                   lr.evidence
            FROM cur
            LEFT JOIN prev ON prev.subindustry = cur.subindustry
            LEFT JOIN latest_rows lr ON lr.subindustry = cur.subindustry
            ORDER BY mention_delta DESC NULLS LAST, cur.mention_count DESC NULLS LAST, cur.research_report_count DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "days": days,
                "limit": limit,
                "has_terms": bool(terms),
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
            },
        ).fetchall()
    emit(rows, {"subindustry": subindustry, "terms": terms, "days": days, "row_count": len(rows), "read_only": True})


def run_company_exposure(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("query") or spec.get("term") or spec.get("subindustry") or "").strip()
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    terms = query_terms(query)
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH latest AS (
              SELECT max(as_of_date) AS dt FROM analytics.ai_hardware_theme_scores
              WHERE score_breakdown->>'forecast_semantics'='earnings-forecast-v2'
            )
            SELECT e.stock_code,e.stock_name,c.name AS subindustry,c.name AS term,c.concept_type AS category,
                   e.as_of_date,round(e.score::numeric,3) AS exposure_score,e.relation_type,
                   e.mention_count_30d,e.evidence_count_90d,e.research_report_count_180d,
                   f.latest_report_date,f.forecast_report_date,e.latest_rating,e.latest_rating_change,
                   e.forecast_year,e.forecast_eps,e.next_forecast_year,e.next_forecast_eps,
                   round(e.forecast_eps_growth::numeric,4) AS forecast_eps_growth,
                   f.forecast_net_profit, f.consensus_eps,f.consensus_net_profit,
                   'CNY/share'::text AS eps_unit,'CNY'::text AS net_profit_unit,
                   e.forward_pe,'source_report_date'::text AS pe_price_basis,
                   c.ai_hardware_relevance AS term_relevance,c.aliases,left(c.description,240) AS relevance_reason,e.evidence
            FROM analytics.ai_hardware_theme_scores e JOIN latest l ON e.as_of_date=l.dt
            JOIN kg.concepts c USING(concept_id)
            LEFT JOIN analytics.asset_forecast_daily f ON f.stock_code=e.stock_code AND f.as_of_date=e.as_of_date
            WHERE e.score_breakdown->>'forecast_semantics'='earnings-forecast-v2'
              AND (%(stock_code)s='' OR e.stock_code=%(stock_code)s)
              AND (%(stock_name)s='' OR e.stock_name ILIKE '%%'||%(stock_name)s||'%%')
              AND (%(has_terms)s=false OR c.name ILIKE ANY(%(patterns)s)
                   OR c.normalized_name ILIKE ANY(%(patterns)s) OR array_to_string(c.aliases,',') ILIKE ANY(%(patterns)s))
            ORDER BY e.score DESC NULLS LAST,e.mention_count_30d DESC,e.stock_code,c.name
            LIMIT %(limit)s
            """,
            {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "has_terms": bool(terms),
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
                "limit": limit,
            },
        ).fetchall()
    emit(rows, {"query": query, "terms": terms, "stock_code": stock_code, "stock_name": stock_name, "row_count": len(rows), "read_only": True})


def run_forecast_trends(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    requested_stock_code = stock_code
    stock_name = str(spec.get("stock_name") or "").strip()
    subindustry = str(spec.get("subindustry") or spec.get("theme") or "").strip()
    terms = query_terms(subindustry)
    days = clamp_int(spec.get("days"), 180, 1, 3650)
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        if stock_code:
            alias = conn.execute(
                "SELECT new_code FROM market.research_security_code_aliases WHERE old_code=%s",
                (stock_code,),
            ).fetchone()
            if alias:
                stock_code = alias["new_code"]
        rows = conn.execute(
            """
            WITH latest_report AS (
              SELECT max(report_date) AS dt FROM research.reports_resolved
            ),
            theme_pool AS (
              SELECT DISTINCT e.stock_code
              FROM analytics.ai_hardware_theme_scores e JOIN kg.concepts tc USING(concept_id)
              WHERE e.as_of_date = (SELECT max(as_of_date) FROM analytics.ai_hardware_theme_scores)
                AND (%(has_terms)s = false OR tc.name ILIKE ANY(%(patterns)s))
            ),
            base AS (
              SELECT r.stock_code, r.stock_name, r.report_date, r.institution, r.rating, r.rating_change,
                     r.document_id, r.report_title, f.forecast_year, f.revenue, f.net_profit, f.eps, f.pe, f.currency, f.confidence
              FROM research.report_forecasts_usable f
              JOIN research.reports_resolved r ON r.document_id = f.document_id
              JOIN latest_report lr ON r.report_date BETWEEN lr.dt - (%(days)s::int - 1) AND lr.dt
              WHERE r.stock_code IS NOT NULL
                AND (%(stock_code)s = '' OR r.stock_code = %(stock_code)s)
                AND (%(stock_name)s = '' OR r.stock_name ILIKE '%%' || %(stock_name)s || '%%')
                AND (%(has_terms)s = false OR r.stock_code IN (SELECT stock_code FROM theme_pool))
            ),
            first_rows AS (
              SELECT DISTINCT ON (stock_code, institution, forecast_year)
                     stock_code, institution, forecast_year, report_date, revenue, net_profit, eps, pe
              FROM base
              ORDER BY stock_code, institution, forecast_year, report_date ASC,document_id
            ),
            latest_rows AS (
              SELECT DISTINCT ON (stock_code, institution, forecast_year)
                     stock_code, stock_name, forecast_year, report_date, institution, rating, rating_change,
                     report_title, revenue, net_profit, eps, pe, currency
              FROM base
              ORDER BY stock_code, institution, forecast_year, report_date DESC,document_id
            ),
            agg AS (
              SELECT stock_code, max(stock_name) AS stock_name, institution, forecast_year,
                     count(*)::int AS forecast_count,
                     count(DISTINCT institution)::int AS institution_count,
                     min(report_date) AS first_report_date,
                     max(report_date) AS latest_report_date,
                     round(avg(revenue)::numeric, 3) AS avg_revenue,
                     round(avg(net_profit)::numeric, 3) AS avg_net_profit,
                     round(avg(eps)::numeric, 4) AS avg_eps,
                     round(avg(pe)::numeric, 3) AS avg_pe
              FROM base
              GROUP BY stock_code, institution, forecast_year
            )
            SELECT a2.stock_code, a2.stock_name, a2.forecast_year,
                   a2.forecast_count, a2.institution_count,
                   a2.first_report_date, a2.latest_report_date,
                   fr.revenue AS first_revenue, lr.revenue AS latest_revenue,
                   CASE WHEN fr.revenue IS NOT NULL AND fr.revenue <> 0 AND lr.revenue IS NOT NULL
                        THEN round(((lr.revenue - fr.revenue) / abs(fr.revenue))::numeric, 4)
                        ELSE NULL END AS revenue_revision_pct,
                   fr.net_profit AS first_net_profit, lr.net_profit AS latest_net_profit,
                   CASE WHEN fr.net_profit IS NOT NULL AND fr.net_profit <> 0 AND lr.net_profit IS NOT NULL
                        THEN round(((lr.net_profit - fr.net_profit) / abs(fr.net_profit))::numeric, 4)
                        ELSE NULL END AS net_profit_revision_pct,
                   fr.eps AS first_eps, lr.eps AS latest_eps,
                   'CNY/share'::text AS eps_unit, 'CNY'::text AS net_profit_unit,
                   CASE WHEN fr.eps>0 AND fr.report_date<lr.report_date AND
                     (SELECT split_factor FROM market.adjustment_factors_daily a JOIN market.assets ma USING(asset_id)
                      WHERE ma.symbol=a2.stock_code AND a.dt<=fr.report_date ORDER BY a.dt DESC LIMIT 1)=
                     (SELECT split_factor FROM market.adjustment_factors_daily a JOIN market.assets ma USING(asset_id)
                      WHERE ma.symbol=a2.stock_code AND a.dt<=lr.report_date ORDER BY a.dt DESC LIMIT 1)
                   THEN round((lr.eps/fr.eps-1)::numeric,4) END AS eps_revision_pct,
                   fr.pe AS first_pe, lr.pe AS latest_pe,
                   a2.avg_revenue, a2.avg_net_profit, a2.avg_eps, a2.avg_pe,
                   lr.institution AS latest_institution,
                   lr.rating AS latest_rating,
                   lr.rating_change AS latest_rating_change,
                   lr.report_title AS latest_report_title,
                   lr.currency
            FROM agg a2
            JOIN first_rows fr ON fr.stock_code = a2.stock_code AND fr.forecast_year = a2.forecast_year AND fr.institution = a2.institution
            JOIN latest_rows lr ON lr.stock_code = a2.stock_code AND lr.forecast_year = a2.forecast_year AND lr.institution = a2.institution
            ORDER BY eps_revision_pct DESC NULLS LAST,
                     a2.forecast_count DESC,
                     a2.latest_report_date DESC
            LIMIT %(limit)s
            """,
            {
                "days": days,
                "stock_code": stock_code,
                "stock_name": stock_name,
                "has_terms": bool(terms),
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
                "limit": limit,
            },
        ).fetchall()
    emit(rows, {"stock_code": stock_code, "requested_stock_code": requested_stock_code, "stock_name": stock_name, "subindustry": subindustry, "terms": terms, "days": days, "row_count": len(rows), "read_only": True})


def run_event_study(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("query") or "").strip()
    terms = query_terms(query, spec.get("keywords"))
    event_type = str(spec.get("event_type") or "").strip()
    stock_code = str(spec.get("stock_code") or "").strip()
    days = clamp_int(spec.get("days"), 90, 1, 3650)
    post_days = clamp_int(spec.get("post_days"), 5, 1, 60)
    limit = clamp_int(spec.get("limit"), 80, 1, 300)
    candidate_limit = clamp_int(spec.get("candidate_limit"), max(limit * 5, 20), 5, 500)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH latest AS (
              SELECT max(event_date) AS dt FROM analytics.document_events
            ),
            event_base AS (
              SELECT row_number() OVER () AS event_id, *
              FROM (
                SELECT e.document_id, e.event_date, e.event_type, e.polarity,
                       e.impact_score, e.confidence, e.summary, e.evidence,
                       e.keywords, e.industries, sc.stock_code,
                       (
                         SELECT count(*)::int
                         FROM unnest(%(terms)s::text[]) AS qt(term)
                         WHERE qt.term = ANY(e.keywords) OR qt.term = ANY(e.industries)
                       ) AS match_count
                FROM analytics.document_events e
                CROSS JOIN LATERAL unnest(e.stock_codes) AS sc(stock_code)
                JOIN latest l ON e.event_date BETWEEN l.dt - (%(days)s::int - 1) AND l.dt
                WHERE (%(event_type)s = '' OR e.event_type = %(event_type)s)
                  AND (%(stock_code)s = '' OR e.stock_codes @> ARRAY[%(stock_code)s]::text[])
                  AND (%(stock_code)s = '' OR sc.stock_code = %(stock_code)s)
                  AND (
                    %(has_terms)s = false
                    OR e.keywords && %(terms)s::text[]
                    OR e.industries && %(terms)s::text[]
                  )
                  AND (
                    %(has_terms)s = false
                    OR (
                      SELECT count(*)::int
                      FROM unnest(%(terms)s::text[]) AS qt(term)
                      WHERE qt.term = ANY(e.keywords) OR qt.term = ANY(e.industries)
                    ) >= CASE WHEN cardinality(%(terms)s::text[]) >= 2 THEN 2 ELSE 1 END
                  )
                ORDER BY match_count DESC NULLS LAST, e.event_date DESC, e.impact_score DESC NULLS LAST, e.confidence DESC NULLS LAST
                LIMIT %(candidate_limit)s
              ) x
            ),
            latest_tech AS (
              SELECT max(dt) AS dt FROM analytics.asset_technical_daily
            ),
            priced AS (
              SELECT eb.*, a.name AS stock_name,
                     t.dt AS base_dt,
                     t.dt AS post_dt,
                     t.close AS base_close,
                     t.close AS post_close,
                     CASE WHEN %(post_days)s::int <= 1 THEN t.pct_chg_1d
                          WHEN %(post_days)s::int <= 7 THEN t.ret_5d
                          ELSE t.ret_20d END AS return_proxy
              FROM event_base eb
              JOIN market.assets a ON a.asset_type = 'stock' AND a.symbol = eb.stock_code
              LEFT JOIN latest_tech lt ON true
              LEFT JOIN analytics.asset_technical_daily t ON t.asset_id = a.asset_id AND t.dt = lt.dt
            ),
            detailed AS (
              SELECT 'detail' AS section,
                     stock_code, stock_name, event_date, event_type, polarity,
                     round(impact_score::numeric, 3) AS impact_score,
                     round(confidence::numeric, 3) AS confidence,
                     base_dt, post_dt,
                     round(base_close::numeric, 3) AS base_close,
                     round(post_close::numeric, 3) AS post_close,
                     round(return_proxy::numeric, 4) AS post_return,
                     keywords, industries,
                     left(summary, 260) AS summary,
                     left(evidence, 320) AS evidence
              FROM priced
            ),
            summary AS (
              SELECT 'summary' AS section,
                     NULL::text AS stock_code,
                     NULL::text AS stock_name,
                     NULL::date AS event_date,
                     coalesce(event_type, 'all') AS event_type,
                     NULL::smallint AS polarity,
                     round(avg(impact_score)::numeric, 3) AS impact_score,
                     round(avg(confidence)::numeric, 3) AS confidence,
                     NULL::date AS base_dt,
                     NULL::date AS post_dt,
                     NULL::numeric AS base_close,
                     NULL::numeric AS post_close,
                     round(avg(return_proxy)::numeric, 4) AS post_return,
                     NULL::text[] AS keywords,
                     NULL::text[] AS industries,
                     ('events=' || count(*) || ', priced=' || count(post_close)) AS summary,
                     NULL::text AS evidence
              FROM priced
              GROUP BY event_type
            )
            SELECT * FROM summary
            UNION ALL
            SELECT * FROM detailed
            ORDER BY section DESC, post_return DESC NULLS LAST, event_date DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "days": days,
                "post_days": post_days,
                "event_type": event_type,
                "stock_code": stock_code,
                "has_terms": bool(terms),
                "patterns": [f"%{term}%" for term in terms] or ["%%"],
                "terms": terms,
                "limit": limit,
                "candidate_limit": candidate_limit,
            },
        ).fetchall()
    emit(rows, {"query": query, "terms": terms, "event_type": event_type, "stock_code": stock_code, "days": days, "post_days": post_days, "candidate_limit": candidate_limit, "row_count": len(rows), "return_method": "latest_asset_technical_return_proxy", "note": "Exact event-date bar lookup is intentionally avoided because online market.bars_daily probing is too slow; build a materialized event-return table for exact event studies.", "read_only": True})


def run_financial_growth_screen(root: str, spec: dict[str, Any]) -> None:
    report_period = str(spec.get("report_period") or "2026Q1").strip().upper()
    metric_name = metric_name_from_arg(spec.get("metric"))
    growth_gt = normalize_pct_threshold(spec.get("growth_gt"), 0.5)
    growth_lt = normalize_pct_threshold(spec.get("growth_lt"))
    return_lte = normalize_pct_threshold(spec.get("return_lte"))
    return_gte = normalize_pct_threshold(spec.get("return_gte"))
    latest_date = str(spec.get("latest_date") or "").strip()
    price_source, adjustment = price_source_from_adjustment(spec.get("price_adjustment"))
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH candidates AS (
              SELECT m.id, m.asset_id, m.stock_code, m.stock_name, m.report_period, m.report_date,
                     m.metric_name, m.metric_value, m.metric_yoy, m.source_document_id,
                     m.source_text, m.confidence, m.metadata,
                     er.base_trade_date, er.latest_trade_date,
                     er.base_close, er.latest_close, er.return_pct,
                     er.price_source, er.adjustment, er.adjustment_fallback, er.data_quality,
                     row_number() OVER (
                       PARTITION BY m.stock_code
                       ORDER BY m.confidence DESC NULLS LAST, m.report_date DESC NULLS LAST, m.id DESC
                     ) AS rn
              FROM company.financial_statement_metrics m
              LEFT JOIN analytics.asset_event_return er
                ON er.stock_code = m.stock_code
               AND er.source_document_id IS NOT DISTINCT FROM m.source_document_id
               AND er.event_type = 'financial_report'
               AND er.event_date = m.report_date
               AND er.price_source = %(price_source)s
              WHERE m.report_period = %(report_period)s
                AND m.metric_name = %(metric_name)s
                AND (%(growth_gt)s::float8 IS NULL OR m.metric_yoy > %(growth_gt)s)
                AND (%(growth_lt)s::float8 IS NULL OR m.metric_yoy < %(growth_lt)s)
            )
            SELECT stock_code, stock_name, report_period, report_date,
                   metric_name,
                   round(metric_value::numeric, 4) AS metric_value,
                   round(metric_yoy::numeric, 4) AS metric_yoy,
                   round((metric_yoy * 100)::numeric, 2) AS metric_yoy_pct,
                   base_trade_date, latest_trade_date,
                   round(base_close::numeric, 3) AS base_close,
                   round(latest_close::numeric, 3) AS latest_close,
                   round(return_pct::numeric, 4) AS return_pct,
                   round((return_pct * 100)::numeric, 2) AS return_pct_points,
                   price_source, adjustment, adjustment_fallback, data_quality,
                   round(confidence::numeric, 3) AS confidence,
                   metadata->>'report_title' AS report_title,
                   source_document_id,
                   left(source_text, 360) AS source_text
            FROM candidates
            WHERE rn = 1
              AND (%(return_lte)s::float8 IS NULL OR return_pct <= %(return_lte)s)
              AND (%(return_gte)s::float8 IS NULL OR return_pct >= %(return_gte)s)
              AND (%(latest_date)s = '' OR latest_trade_date <= %(latest_date)s::date)
            ORDER BY return_pct ASC NULLS LAST, metric_yoy DESC NULLS LAST, report_date DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "report_period": report_period,
                "metric_name": metric_name,
                "growth_gt": growth_gt,
                "growth_lt": growth_lt,
                "return_lte": return_lte,
                "return_gte": return_gte,
                "latest_date": latest_date,
                "price_source": price_source,
                "limit": limit,
            },
        ).fetchall()
        freshness = conn.execute(
            """
            SELECT max(report_date) AS latest_metric_report_date,
                   max(updated_at) AS metrics_updated_at
            FROM company.financial_statement_metrics
            WHERE report_period = %(report_period)s
            """,
            {"report_period": report_period},
        ).fetchone()
        event_freshness = conn.execute(
            """
            SELECT max(latest_trade_date) AS latest_event_return_trade_date,
                   max(updated_at) AS event_returns_updated_at
            FROM analytics.asset_event_return
            WHERE event_type = 'financial_report'
              AND price_source = %(price_source)s
            """,
            {"price_source": price_source},
        ).fetchone()
        stats = conn.execute(
            """
            WITH candidates AS (
              SELECT m.stock_code, er.return_pct, er.data_quality
              FROM company.financial_statement_metrics m
              LEFT JOIN analytics.asset_event_return er
                ON er.stock_code = m.stock_code
               AND er.source_document_id IS NOT DISTINCT FROM m.source_document_id
               AND er.event_type = 'financial_report'
               AND er.event_date = m.report_date
               AND er.price_source = %(price_source)s
              WHERE m.report_period = %(report_period)s
                AND m.metric_name = %(metric_name)s
                AND (%(growth_gt)s::float8 IS NULL OR m.metric_yoy > %(growth_gt)s)
                AND (%(growth_lt)s::float8 IS NULL OR m.metric_yoy < %(growth_lt)s)
            )
            SELECT count(DISTINCT stock_code)::int AS strict_metric_candidate_count,
                   count(DISTINCT stock_code) FILTER (WHERE return_pct IS NOT NULL)::int AS priced_candidate_count,
                   count(DISTINCT stock_code) FILTER (WHERE return_pct IS NULL)::int AS unpriced_candidate_count,
                   count(DISTINCT stock_code) FILTER (WHERE data_quality = 'missing_price')::int AS missing_price_candidate_count,
                   (SELECT count(*)::int FROM market.assets WHERE asset_type='stock') AS universe_count,
                   (SELECT count(DISTINCT stock_code)::int
                      FROM company.financial_statement_metrics
                     WHERE report_period=%(report_period)s AND metric_name=%(metric_name)s) AS covered_count
            FROM candidates
            """,
            {
                "report_period": report_period,
                "metric_name": metric_name,
                "growth_gt": growth_gt,
                "growth_lt": growth_lt,
                "price_source": price_source,
            },
        ).fetchone()
    universe_count = int((stats or {}).get("universe_count") or 0)
    covered_count = int((stats or {}).get("covered_count") or 0)
    meta = {
        "report_period": report_period,
        "metric_name": metric_name,
        "growth_gt": growth_gt,
        "growth_lt": growth_lt,
        "return_lte": return_lte,
        "return_gte": return_gte,
        "price_source": price_source,
        "adjustment": adjustment,
        "latest_date_filter": latest_date,
        "row_count": len(rows),
        "read_only": True,
        "universe_count": universe_count,
        "covered_count": covered_count,
        "coverage_pct": round(100 * covered_count / universe_count, 2) if universe_count else 0,
        "metric_definition": "Actual disclosed quarterly metric yoy from company.financial_statement_metrics; no forecast substitution.",
        "warnings": ([f"Actual {report_period} {metric_name} coverage is {covered_count}/{universe_count}; results are not a complete all-market screen."]
                     if covered_count < universe_count else []),
        "data_scope": "actual extracted quarterly metrics from company.financial_statement_metrics; not research.report_forecasts",
        **(freshness or {}),
        **(event_freshness or {}),
        **(stats or {}),
    }
    emit(rows, meta)


def run_event_return(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    event_type = str(spec.get("event_type") or "financial_report").strip()
    event_date = str(spec.get("event_date") or "").strip()
    price_source, adjustment = price_source_from_adjustment(spec.get("price_adjustment"))
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            SELECT er.stock_code, er.stock_name, er.event_type, er.event_date,
                   er.base_trade_date, er.latest_trade_date,
                   round(er.base_close::numeric, 3) AS base_close,
                   round(er.latest_close::numeric, 3) AS latest_close,
                   round(er.return_pct::numeric, 4) AS return_pct,
                   round((er.return_pct * 100)::numeric, 2) AS return_pct_points,
                   er.price_source, er.adjustment, er.adjustment_fallback, er.data_quality,
                   er.source_document_id,
                   m.report_period,
                   m.metric_name,
                   round(m.metric_yoy::numeric, 4) AS metric_yoy,
                   round((m.metric_yoy * 100)::numeric, 2) AS metric_yoy_pct,
                   m.metadata->>'report_title' AS report_title,
                   left(m.source_text, 300) AS source_text
            FROM analytics.asset_event_return er
            LEFT JOIN company.financial_statement_metrics m
              ON m.stock_code = er.stock_code
             AND m.source_document_id IS NOT DISTINCT FROM er.source_document_id
             AND m.report_date = er.event_date
             AND m.metric_name = 'net_profit_parent'
            WHERE (%(stock_code)s = '' OR er.stock_code = %(stock_code)s)
              AND (%(stock_name)s = '' OR er.stock_name ILIKE '%%' || %(stock_name)s || '%%')
              AND (%(event_type)s = '' OR er.event_type = %(event_type)s)
              AND (%(event_date)s = '' OR er.event_date = %(event_date)s::date)
              AND er.price_source = %(price_source)s
            ORDER BY er.event_date DESC, er.return_pct ASC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "event_type": event_type,
                "event_date": event_date,
                "price_source": price_source,
                "limit": limit,
            },
        ).fetchall()
    emit(
        rows,
        {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "event_type": event_type,
            "event_date": event_date,
            "price_source": price_source,
            "adjustment": adjustment,
            "row_count": len(rows),
            "read_only": True,
            "note": "Uses materialized analytics.asset_event_return. Prices are not mixed across adjustment/source.",
        },
    )


def run_fact_check_stock(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    report_period = str(spec.get("report_period") or "2026Q1").strip().upper()
    price_source, adjustment = price_source_from_adjustment(spec.get("price_adjustment"))
    days = clamp_int(spec.get("days"), 180, 1, 3650)
    limit = clamp_int(spec.get("limit"), 80, 1, 200)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH target AS (
              SELECT DISTINCT stock_code, stock_name
              FROM company.financial_statement_metrics
              WHERE (%(stock_code)s <> '' AND stock_code = %(stock_code)s)
                 OR (%(stock_name)s <> '' AND stock_name ILIKE '%%' || %(stock_name)s || '%%')
              UNION
              SELECT DISTINCT stock_code, stock_name
              FROM research.reports_resolved
              WHERE (%(stock_code)s <> '' AND stock_code = %(stock_code)s)
                 OR (%(stock_name)s <> '' AND stock_name ILIKE '%%' || %(stock_name)s || '%%')
              LIMIT 1
            ),
            metrics AS (
              SELECT 'actual_metric' AS section,
                     jsonb_build_object(
                       'stock_code', m.stock_code,
                       'stock_name', m.stock_name,
                       'report_period', m.report_period,
                       'report_date', m.report_date,
                       'metric_name', m.metric_name,
                       'metric_value', m.metric_value,
                       'metric_yoy', round(m.metric_yoy::numeric, 4),
                       'metric_yoy_pct', round((m.metric_yoy * 100)::numeric, 2),
                       'confidence', m.confidence,
                       'report_title', m.metadata->>'report_title',
                       'source_text', left(m.source_text, 360),
                       'source_document_id', m.source_document_id
                     ) AS data
              FROM company.financial_statement_metrics m
              JOIN target t ON t.stock_code = m.stock_code
              WHERE m.report_period = %(report_period)s
              ORDER BY m.report_date DESC, m.metric_name
              LIMIT 20
            ),
            returns AS (
              SELECT 'event_return' AS section,
                     jsonb_build_object(
                       'stock_code', er.stock_code,
                       'stock_name', er.stock_name,
                       'event_type', er.event_type,
                       'event_date', er.event_date,
                       'base_trade_date', er.base_trade_date,
                       'latest_trade_date', er.latest_trade_date,
                       'base_close', er.base_close,
                       'latest_close', er.latest_close,
                       'return_pct', round(er.return_pct::numeric, 4),
                       'return_pct_points', round((er.return_pct * 100)::numeric, 2),
                       'price_source', er.price_source,
                       'adjustment', er.adjustment,
                       'adjustment_fallback', er.adjustment_fallback,
                       'data_quality', er.data_quality,
                       'source_document_id', er.source_document_id
                     ) AS data
              FROM analytics.asset_event_return er
              JOIN target t ON t.stock_code = er.stock_code
              WHERE er.event_type = 'financial_report'
                AND er.price_source = %(price_source)s
              ORDER BY er.event_date DESC
              LIMIT 20
            ),
            reports AS (
              SELECT 'recent_report' AS section,
                     jsonb_build_object(
                       'stock_code', r.stock_code,
                       'stock_name', r.stock_name,
                       'report_date', r.report_date,
                       'institution', r.institution,
                       'rating', r.rating,
                       'rating_change', r.rating_change,
                       'report_title', r.report_title,
                       'document_id', r.document_id
                     ) AS data
              FROM research.reports_resolved r
              JOIN target t ON t.stock_code = r.stock_code
              WHERE r.report_date >= (SELECT max(report_date) FROM research.reports_resolved) - (%(days)s::int - 1)
              ORDER BY r.report_date DESC
              LIMIT 20
            )
            SELECT * FROM metrics
            UNION ALL
            SELECT * FROM returns
            UNION ALL
            SELECT * FROM reports
            LIMIT %(limit)s
            """,
            {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "report_period": report_period,
                "price_source": price_source,
                "days": days,
                "limit": limit,
            },
        ).fetchall()
    emit(
        rows,
        {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "report_period": report_period,
            "price_source": price_source,
            "adjustment": adjustment,
            "days": days,
            "row_count": len(rows),
            "read_only": True,
            "instruction": "Use actual_metric rows for reported financial growth. Do not substitute forecast rows when actual_metric is missing.",
        },
    )


def run_target_price_history(root: str, spec: dict[str, Any]) -> None:
    stock_codes = list(dict.fromkeys(source_types_arg(spec.get("stock_codes"))))
    stock_code = str(spec.get("stock_code") or "").strip()
    if stock_code and stock_code not in stock_codes:
        stock_codes.append(stock_code)
    stock_name = str(spec.get("stock_name") or "").strip()
    days = clamp_int(spec.get("days"), 365, 1, 3650)
    as_of_date = str(spec.get("as_of_date") or "").strip()
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    requested_count = len(stock_codes) or (1 if stock_name else 0)
    params = {
        "has_codes": bool(stock_codes), "stock_codes": stock_codes,
        "stock_name": stock_name, "as_of_date": as_of_date or None,
        "days": days, "limit": limit,
    }
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH facts AS (
              SELECT DISTINCT ON (rr.document_id, COALESCE(rr.stock_code, r.stock_code), rr.target_price)
                     rr.document_id::text AS document_id,
                     COALESCE(rr.stock_code, r.stock_code) AS stock_code,
                     r.stock_name, r.report_date, r.report_title, r.institution,
                     rr.rating, rr.rating_change, rr.previous_rating,
                     round(rr.target_price::numeric, 4) AS explicit_target_price,
                     rr.extracted_by, round(rr.confidence::numeric, 3) AS confidence,
                     rr.metadata->>'target_currency' currency,
                     rr.metadata->>'target_horizon_months' horizon_months,
                     rr.metadata->>'target_valid_until' valid_until,
                     rr.metadata->>'target_price_basis' price_basis,
                     r.source_url
              FROM research.report_ratings rr
              JOIN research.reports_resolved r ON r.document_id = rr.document_id
              WHERE rr.target_price IS NOT NULL
                AND (%(has_codes)s = false OR COALESCE(rr.stock_code, r.stock_code) = ANY(%(stock_codes)s))
                AND (%(stock_name)s = '' OR r.stock_name ILIKE '%%' || %(stock_name)s || '%%')
                AND (%(as_of_date)s::date IS NULL OR r.report_date <= %(as_of_date)s::date)
                AND r.report_date >= COALESCE(%(as_of_date)s::date, current_date) - (%(days)s * interval '1 day')
              ORDER BY rr.document_id, COALESCE(rr.stock_code, r.stock_code), rr.target_price,
                       rr.confidence DESC NULLS LAST, rr.created_at DESC
            )
            SELECT *, count(*) OVER()::int AS strict_candidate_count
            FROM facts
            ORDER BY report_date DESC NULLS LAST, stock_code, institution
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()
        coverage = conn.execute(
            """
            WITH requested AS (
              SELECT DISTINCT COALESCE(rr.stock_code, r.stock_code) AS stock_code
              FROM research.report_ratings rr
              JOIN research.reports_resolved r ON r.document_id = rr.document_id
              WHERE rr.target_price IS NOT NULL
                AND (%(has_codes)s = false OR COALESCE(rr.stock_code, r.stock_code) = ANY(%(stock_codes)s))
                AND (%(stock_name)s = '' OR r.stock_name ILIKE '%%' || %(stock_name)s || '%%')
                AND (%(as_of_date)s::date IS NULL OR r.report_date <= %(as_of_date)s::date)
                AND r.report_date >= COALESCE(%(as_of_date)s::date, current_date) - (%(days)s * interval '1 day')
            )
            SELECT (SELECT count(*)::int FROM requested) AS matched_stock_count,
                   count(DISTINCT r.stock_code)::int AS report_stocks,
                   count(DISTINCT COALESCE(rr.stock_code, r.stock_code))
                     FILTER (WHERE rr.target_price IS NOT NULL)::int AS target_price_stocks,
                   max(r.report_date) FILTER (WHERE rr.target_price IS NOT NULL) AS latest_target_price_date
            FROM research.reports_resolved r
            LEFT JOIN research.report_ratings rr ON rr.document_id=r.document_id
            WHERE r.report_type='stock'
            """,
            params,
        ).fetchone()
    universe_count = requested_count or int(coverage.get("report_stocks") or 0)
    covered_count = int(coverage.get("matched_stock_count") or 0) if requested_count else int(coverage.get("target_price_stocks") or 0)
    emit(rows, {
        "metric_definition": "Explicit broker target price parsed from research.report_ratings.target_price; never model-implied valuation.",
        "as_of_date": as_of_date or "latest", "days": days,
        "stock_codes": stock_codes, "stock_name": stock_name,
        "universe_count": universe_count, "covered_count": covered_count,
        "coverage_pct": round(100 * covered_count / universe_count, 2) if universe_count else 0,
        "global_report_stock_count": int(coverage.get("report_stocks") or 0),
        "global_target_price_stock_count": int(coverage.get("target_price_stocks") or 0),
        "latest_target_price_date": coverage.get("latest_target_price_date"),
        "strict_candidate_count": rows[0].get("strict_candidate_count", 0) if rows else 0,
        "row_count": len(rows),
        "source_scope": "research.reports + research.report_ratings, explicit target_price only",
        "warnings": ["Missing rows mean no parsed explicit broker target price in the requested window; do not substitute an inferred valuation."],
        "read_only": True,
    })


def _price_sources(adjustment: str) -> list[str]:
    normalized = str(adjustment or "qfq").strip().lower()
    if normalized == "none":
        return ["eastmoney:kline:day:none", "sina:kline:day:none"]
    if normalized == "hfq":
        return ["eastmoney:kline:day:hfq"]
    return ["eastmoney:kline:day:qfq"]


def run_price_series(root: str, spec: dict[str, Any]) -> None:
    stock_codes = list(dict.fromkeys(source_types_arg(spec.get("stock_codes"))))
    stock_code = str(spec.get("stock_code") or "").strip()
    if stock_code and stock_code not in stock_codes:
        stock_codes.append(stock_code)
    stock_name = str(spec.get("stock_name") or "").strip()
    start_date = str(spec.get("start_date") or "").strip()
    end_date = str(spec.get("end_date") or "").strip()
    adjustment = str(spec.get("adjustment") or "qfq").strip().lower()
    adjustment = adjustment if adjustment in {"qfq", "hfq", "none"} else "qfq"
    frequency = str(spec.get("frequency") or "daily").strip().lower()
    frequency = frequency if frequency in {"daily", "weekly", "monthly"} else "daily"
    limit = clamp_int(spec.get("limit"), 240, 1, 500)
    if not stock_codes and not stock_name:
        emit([], {
            "row_count": 0, "coverage_status": "entity_required", "universe_count": 0,
            "covered_count": 0, "coverage_pct": 0, "read_only": True,
            "warnings": ["Provide stock_code, stock_codes, or stock_name."],
        })
        return
    sources = _price_sources(adjustment)
    requested_count = len(stock_codes) or 1
    params = {
        "has_codes": bool(stock_codes), "stock_codes": stock_codes, "stock_name": stock_name,
        "sources": sources, "start_date": start_date or None, "end_date": end_date or None,
        "frequency": frequency, "limit": limit,
    }
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH selected_assets AS (
              SELECT DISTINCT ON (a.symbol) a.asset_id, a.symbol AS stock_code, a.name AS stock_name, a.exchange
              FROM market.assets a
              WHERE a.asset_type='stock'
                AND (%(has_codes)s=false OR a.symbol=ANY(%(stock_codes)s))
                AND (%(stock_name)s='' OR a.name ILIKE '%%' || %(stock_name)s || '%%')
              ORDER BY a.symbol, a.updated_at DESC
              LIMIT 10
            ), ranked AS (
              SELECT a.stock_code, a.stock_name, a.exchange,
                     (b.ts AT TIME ZONE 'Asia/Shanghai')::date AS trade_date,
                     b.open, b.high, b.low, b.close, b.volume, b.amount, b.source,
                     row_number() OVER (
                       PARTITION BY b.asset_id, (b.ts AT TIME ZONE 'Asia/Shanghai')::date
                       ORDER BY array_position(%(sources)s::text[], b.source), b.created_at DESC
                     ) AS source_rank
              FROM selected_assets a
              JOIN market.bars_daily b ON b.asset_id=a.asset_id AND b.source=ANY(%(sources)s)
              WHERE b.ts >= (
                      COALESCE(%(start_date)s::date, COALESCE(%(end_date)s::date, current_date) - 365)::timestamp
                      AT TIME ZONE 'Asia/Shanghai'
                    )
                AND b.ts < (
                      (COALESCE(%(end_date)s::date, current_date) + 1)::timestamp
                      AT TIME ZONE 'Asia/Shanghai'
                    )
                AND (%(start_date)s::date IS NULL OR (b.ts AT TIME ZONE 'Asia/Shanghai')::date >= %(start_date)s::date)
                AND (%(end_date)s::date IS NULL OR (b.ts AT TIME ZONE 'Asia/Shanghai')::date <= %(end_date)s::date)
            ), canonical AS (
              SELECT * FROM ranked WHERE source_rank=1
            ), bucketed AS (
              SELECT stock_code, stock_name, exchange,
                     CASE %(frequency)s WHEN 'weekly' THEN date_trunc('week', trade_date)::date
                          WHEN 'monthly' THEN date_trunc('month', trade_date)::date ELSE trade_date END AS trade_date,
                     (array_agg(open ORDER BY trade_date))[1] AS open,
                     max(high) AS high, min(low) AS low,
                     (array_agg(close ORDER BY trade_date DESC))[1] AS close,
                     sum(volume) AS volume, sum(amount) AS amount,
                     (array_agg(source ORDER BY trade_date DESC))[1] AS source,
                     array_agg(DISTINCT source ORDER BY source) AS contributing_sources,
                     count(*)::int AS trading_days
              FROM canonical
              GROUP BY stock_code, stock_name, exchange,
                       CASE %(frequency)s WHEN 'weekly' THEN date_trunc('week', trade_date)::date
                            WHEN 'monthly' THEN date_trunc('month', trade_date)::date ELSE trade_date END
            )
            SELECT stock_code, stock_name, exchange, trade_date,
                   round(open::numeric,4) AS open, round(high::numeric,4) AS high,
                   round(low::numeric,4) AS low, round(close::numeric,4) AS close,
                   round(volume::numeric,2) AS volume, round(amount::numeric,2) AS amount,
                   source, contributing_sources, trading_days,
                   count(*) OVER()::int AS strict_candidate_count
            FROM bucketed
            ORDER BY trade_date, stock_code
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()
        coverage = conn.execute(
            """
            WITH selected_assets AS (
              SELECT DISTINCT ON (a.symbol) a.asset_id, a.symbol
              FROM market.assets a
              WHERE a.asset_type='stock'
                AND (%(has_codes)s=false OR a.symbol=ANY(%(stock_codes)s))
                AND (%(stock_name)s='' OR a.name ILIKE '%%' || %(stock_name)s || '%%')
              ORDER BY a.symbol, a.updated_at DESC LIMIT 10
            )
            SELECT count(DISTINCT a.symbol)::int AS resolved_count,
                   count(DISTINCT a.symbol) FILTER (WHERE b.asset_id IS NOT NULL)::int AS covered_count,
                   min((b.ts AT TIME ZONE 'Asia/Shanghai')::date) AS first_trade_date,
                   max((b.ts AT TIME ZONE 'Asia/Shanghai')::date) AS latest_trade_date
            FROM selected_assets a
            LEFT JOIN market.bars_daily b ON b.asset_id=a.asset_id AND b.source=ANY(%(sources)s)
              AND b.ts >= (
                    COALESCE(%(start_date)s::date, COALESCE(%(end_date)s::date, current_date) - 365)::timestamp
                    AT TIME ZONE 'Asia/Shanghai'
                  )
              AND b.ts < (
                    (COALESCE(%(end_date)s::date, current_date) + 1)::timestamp
                    AT TIME ZONE 'Asia/Shanghai'
                  )
              AND (%(start_date)s::date IS NULL OR (b.ts AT TIME ZONE 'Asia/Shanghai')::date >= %(start_date)s::date)
              AND (%(end_date)s::date IS NULL OR (b.ts AT TIME ZONE 'Asia/Shanghai')::date <= %(end_date)s::date)
            """,
            params,
        ).fetchone()
    covered_count = int(coverage.get("covered_count") or 0)
    emit(rows, {
        "metric_definition": "Canonical OHLCV by Asia/Shanghai trade date; one preferred source per asset/date before optional aggregation.",
        "stock_codes": stock_codes, "stock_name": stock_name,
        "start_date": start_date or coverage.get("first_trade_date"),
        "end_date": end_date or coverage.get("latest_trade_date"),
        "as_of_date": coverage.get("latest_trade_date"),
        "adjustment": adjustment, "frequency": frequency, "source_priority": sources,
        "universe_count": requested_count, "resolved_count": int(coverage.get("resolved_count") or 0),
        "covered_count": covered_count,
        "coverage_pct": round(100 * covered_count / requested_count, 2) if requested_count else 0,
        "strict_candidate_count": rows[0].get("strict_candidate_count", 0) if rows else 0,
        "row_count": len(rows), "source_scope": "market.bars_daily",
        "warnings": ["Dates are canonical Asia/Shanghai trade dates; never display raw UTC b.ts as the trading date."],
        "read_only": True,
    })


def run_limit_up_list(root: str, spec: dict[str, Any]) -> None:
    as_of_date = str(spec.get("as_of_date") or "").strip()
    exclude_ipo_window = bool(spec.get("exclude_ipo_window", True))
    limit = clamp_int(spec.get("limit"), 200, 1, 500)
    sources = _price_sources("none")
    params = {"sources": sources, "as_of_date": as_of_date or None, "exclude_ipo_window": exclude_ipo_window, "limit": limit}
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH ranked AS (
              SELECT a.asset_id, a.symbol AS stock_code, a.name AS stock_name, a.exchange, a.listed_date,
                     (b.ts AT TIME ZONE 'Asia/Shanghai')::date AS trade_date,
                     b.close, b.source,
                     row_number() OVER (
                       PARTITION BY b.asset_id, (b.ts AT TIME ZONE 'Asia/Shanghai')::date
                       ORDER BY array_position(%(sources)s::text[], b.source), b.created_at DESC
                     ) AS source_rank
              FROM market.assets a
              JOIN market.bars_daily b ON b.asset_id=a.asset_id AND b.source=ANY(%(sources)s)
              WHERE a.asset_type='stock'
                AND b.ts >= (
                      (COALESCE(%(as_of_date)s::date, current_date) - 20)::timestamp
                      AT TIME ZONE 'Asia/Shanghai'
                    )
                AND b.ts < (
                      (COALESCE(%(as_of_date)s::date, current_date) + 1)::timestamp
                      AT TIME ZONE 'Asia/Shanghai'
                    )
                AND (b.ts AT TIME ZONE 'Asia/Shanghai')::date <= COALESCE(%(as_of_date)s::date, current_date)
            ), canonical AS (
              SELECT *, lag(close) OVER (PARTITION BY asset_id ORDER BY trade_date) AS previous_close
              FROM ranked WHERE source_rank=1
            ), selected_date AS (
              SELECT max(trade_date) AS dt FROM canonical
            ), classified AS (
              SELECT c.*,
                     CASE WHEN c.stock_name ~* '(^|\\*)ST' THEN 0.05
                          WHEN c.stock_code LIKE '300%%' OR c.stock_code LIKE '301%%'
                            OR c.stock_code LIKE '688%%' OR c.stock_code LIKE '689%%' THEN 0.20
                          WHEN c.stock_code LIKE '8%%' OR c.stock_code LIKE '4%%'
                            OR c.stock_code LIKE '92%%' THEN 0.30
                          ELSE 0.10 END AS limit_ratio
              FROM canonical c JOIN selected_date d ON c.trade_date=d.dt
              WHERE c.previous_close IS NOT NULL
            )
            SELECT stock_code, stock_name, exchange, trade_date,
                   round(previous_close::numeric,4) AS previous_close,
                   round(close::numeric,4) AS close,
                   round((close / previous_close - 1)::numeric,6) AS pct_change,
                   limit_ratio, round((previous_close * (1 + limit_ratio))::numeric,2) AS theoretical_limit_price,
                   source, listed_date, count(*) OVER()::int AS strict_candidate_count
            FROM classified
            WHERE abs(close - round((previous_close * (1 + limit_ratio))::numeric,2)) <= 0.0051
              AND (%(exclude_ipo_window)s=false OR listed_date IS NULL OR trade_date > listed_date + 7)
            ORDER BY pct_change DESC, stock_code
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()
        coverage = conn.execute(
            """
            WITH ranked AS (
              SELECT b.asset_id, (b.ts AT TIME ZONE 'Asia/Shanghai')::date AS trade_date,
                     row_number() OVER (PARTITION BY b.asset_id, (b.ts AT TIME ZONE 'Asia/Shanghai')::date
                                        ORDER BY array_position(%(sources)s::text[], b.source), b.created_at DESC) AS rn
              FROM market.bars_daily b JOIN market.assets a ON a.asset_id=b.asset_id
              WHERE a.asset_type='stock' AND b.source=ANY(%(sources)s)
                AND b.ts >= (
                      (COALESCE(%(as_of_date)s::date, current_date) - 20)::timestamp
                      AT TIME ZONE 'Asia/Shanghai'
                    )
                AND b.ts < (
                      (COALESCE(%(as_of_date)s::date, current_date) + 1)::timestamp
                      AT TIME ZONE 'Asia/Shanghai'
                    )
                AND (b.ts AT TIME ZONE 'Asia/Shanghai')::date <= COALESCE(%(as_of_date)s::date, current_date)
            ), d AS (SELECT max(trade_date) AS dt FROM ranked WHERE rn=1)
            SELECT d.dt AS as_of_date,
                   (SELECT count(*)::int FROM market.assets WHERE asset_type='stock') AS universe_count,
                   count(DISTINCT asset_id)::int AS covered_count
            FROM ranked r CROSS JOIN d WHERE r.rn=1 AND r.trade_date=d.dt GROUP BY d.dt
            """,
            params,
        ).fetchone() or {}
    universe_count = int(coverage.get("universe_count") or 0)
    covered_count = int(coverage.get("covered_count") or 0)
    emit(rows, {
        "metric_definition": "Rule-based A-share limit-up close versus previous unadjusted close: ST 5%, ChiNext/STAR 20%, Beijing 30%, other 10%.",
        "requested_date": as_of_date or "latest_on_or_before_today", "as_of_date": coverage.get("as_of_date"),
        "universe_count": universe_count, "covered_count": covered_count,
        "coverage_pct": round(100 * covered_count / universe_count, 2) if universe_count else 0,
        "strict_candidate_count": rows[0].get("strict_candidate_count", 0) if rows else 0,
        "row_count": len(rows), "source_scope": "market.bars_daily unadjusted sources only",
        "source_priority": sources, "exclude_ipo_window": exclude_ipo_window,
        "warnings": ["Stocks within seven calendar days of listing are excluded by default as a conservative IPO-window proxy. This is not an exchange-certified limit-up feed."],
        "read_only": True,
    })


def run_data_health(root: str, spec: dict[str, Any]) -> None:
    limit = clamp_int(spec.get("limit"), 50, 1, 200)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH table_estimates AS (
              SELECT n.nspname AS schema_name, c.relname AS table_name, greatest(c.reltuples, 0)::bigint AS estimated_rows
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE (n.nspname, c.relname) IN (
                ('market', 'bars_daily'),
                ('market', 'bars_1m')
              )
            ),
            freshness AS (
              SELECT 'freshness' AS section, 'corpus.documents' AS metric,
                     max(published_at)::text AS latest_value, sum(cnt)::text AS count_value,
                     jsonb_build_object('source_types', jsonb_object_agg(source_type, cnt)) AS details
              FROM (
                SELECT source_type, count(*) AS cnt, max(published_at) AS published_at
                FROM corpus.documents
                GROUP BY source_type
              ) x
              UNION ALL
              SELECT 'freshness', 'research.reports', max(report_date)::text, sum(cnt)::text,
                     jsonb_build_object('ocr_status', jsonb_object_agg(coalesce(ocr_status, 'null'), cnt))
              FROM (
                SELECT report_date, ocr_status, count(*) AS cnt
                FROM research.reports_resolved
                GROUP BY report_date, ocr_status
              ) x
              UNION ALL
              SELECT 'freshness', 'market.bars_daily',
                     (SELECT max(last_cursor) FROM market.crawl_state WHERE job_name ILIKE '%%daily%%' OR target_key ILIKE '%%daily%%'),
                     coalesce((SELECT nullif(estimated_rows, 0)::text FROM table_estimates WHERE schema_name = 'market' AND table_name = 'bars_daily'), 'unknown'),
                     jsonb_build_object('count_is_estimated', true, 'latest_is_crawl_cursor', true)
              UNION ALL
              SELECT 'freshness', 'market.bars_1m',
                     (SELECT max(last_cursor) FROM market.crawl_state WHERE job_name ILIKE '%%1m%%' OR target_key ILIKE '%%1m%%' OR target_key ILIKE '%%minute%%'),
                     coalesce((SELECT nullif(estimated_rows, 0)::text FROM table_estimates WHERE schema_name = 'market' AND table_name = 'bars_1m'), 'unknown'),
                     jsonb_build_object('count_is_estimated', true, 'latest_is_crawl_cursor', true)
              UNION ALL
              SELECT 'freshness', 'analytics.document_events', max(event_date)::text, count(*)::text, '{}'::jsonb
              FROM analytics.document_events
              UNION ALL
              SELECT 'freshness', 'analytics.ai_hardware_mentions', max(mention_date)::text, count(*)::text, '{}'::jsonb
              FROM analytics.ai_hardware_mentions
              UNION ALL
              SELECT 'freshness', 'analytics.asset_signal_daily', max(dt)::text, count(*)::text, '{}'::jsonb
              FROM analytics.asset_signal_daily
              UNION ALL
              SELECT 'freshness', 'analytics.asset_snapshot_daily', max(as_of_date)::text, count(*)::text,
                     jsonb_build_object('latest_updated_at', max(updated_at))
              FROM analytics.asset_snapshot_daily
              UNION ALL
              SELECT 'freshness', 'company.financial_metrics', max(report_date)::text, count(*)::text,
                     jsonb_build_object(
                       'covered_assets', count(DISTINCT asset_id),
                       'metric_count', count(DISTINCT metric_name)
                     )
              FROM company.financial_metrics
              UNION ALL
              SELECT 'freshness', 'market.money_flow_daily', max(ts)::text, count(*)::text,
                     jsonb_build_object('covered_assets', count(DISTINCT asset_id))
              FROM market.money_flow_daily
              UNION ALL
              SELECT 'freshness', 'market.events_raw', max(latest_date)::text, sum(cnt)::text,
                     jsonb_build_object('category_watermarks', jsonb_object_agg(category, latest_date))
              FROM (
                SELECT category, max(event_date)::text AS latest_date, count(*) AS cnt
                FROM market.events_raw GROUP BY category
              ) event_health
              UNION ALL
              SELECT 'freshness', 'macro.indicator_values', max(v.observed_at)::text, count(*)::text,
                     jsonb_build_object(
                       'indicator_count', count(DISTINCT i.indicator_code),
                       'non_null_value_count', count(v.value)
                     )
              FROM macro.indicator_values v JOIN macro.indicators i USING(indicator_id)
              UNION ALL
              SELECT 'freshness', 'corpus.document_embeddings', max(e.created_at)::text, count(*)::text,
                     jsonb_build_object(
                       'embedded_documents', count(DISTINCT e.document_id),
                       'dimensions', max(e.dimensions),
                       'models', array_agg(DISTINCT e.embedding_model)
                     )
              FROM corpus.document_embeddings e
              UNION ALL
              SELECT 'freshness', 'ops.document_embedding_queue', max(coalesce(processed_at, dirty_at))::text, count(*)::text,
                     jsonb_build_object(
                       'ok', count(*) FILTER (WHERE status='ok'),
                       'pending', count(*) FILTER (WHERE status='pending'),
                       'running', count(*) FILTER (WHERE status='running'),
                       'failed', count(*) FILTER (WHERE status='failed'),
                       'skipped', count(*) FILTER (WHERE status='skipped')
                     )
              FROM ops.document_embedding_queue
              UNION ALL
              SELECT 'freshness', 'corpus.document_search_index', max(indexed_at)::text, count(*)::text,
                     jsonb_build_object('latest_source_updated_at', max(source_updated_at))
              FROM corpus.document_search_index
              UNION ALL
              SELECT 'freshness', 'kg.market_entity_relations', max(updated_at)::text, count(*)::text,
                     jsonb_build_object(
                       'active', count(*) FILTER (WHERE status='active'),
                       'relation_types', count(DISTINCT relation_type)
                     )
              FROM kg.market_entity_relations
              UNION ALL
              SELECT 'freshness', 'analytics.ai_hardware_subindustry_daily', max(dt)::text, count(*)::text, '{}'::jsonb
              FROM analytics.ai_hardware_subindustry_daily
            ),
            ocr AS (
              SELECT 'ops' AS section, 'ops.ocr_queue:' || coalesce(status, 'null') AS metric,
                     max(updated_at)::text AS latest_value, sum(cnt)::text AS count_value,
                     jsonb_build_object('report_types', jsonb_object_agg(coalesce(report_type, 'null'), cnt)) AS details
              FROM (
                SELECT status, report_type, max(updated_at) AS updated_at, count(*) AS cnt
                FROM ops.ocr_queue
                GROUP BY status, report_type
              ) x
              GROUP BY status
            ),
            search_queue AS (
              SELECT 'ops' AS section, 'ops.document_index_queue:' || status AS metric,
                     max(coalesce(indexed_at, dirty_at))::text AS latest_value,
                     count(*)::text AS count_value,
                     jsonb_build_object(
                       'max_retry_count', max(retry_count),
                       'oldest_dirty_at', min(dirty_at),
                       'last_error', left((array_agg(last_error ORDER BY dirty_at DESC))[1], 500)
                     ) AS details
              FROM ops.document_index_queue
              GROUP BY status
            ),
            runs AS (
              SELECT 'ops' AS section, 'ops.crawler_runs:' || task_name || ':' || source_name AS metric,
                     max(coalesce(finished_at, started_at))::text AS latest_value,
                     count(*)::text AS count_value,
                     jsonb_build_object(
                       'latest_status', (array_agg(status ORDER BY started_at DESC))[1],
                       'latest_error', left((array_agg(error ORDER BY started_at DESC))[1], 500),
                       'new_rows_7d', sum(new_rows) FILTER (WHERE started_at >= now() - interval '7 days')
                     ) AS details
              FROM ops.crawler_runs
              GROUP BY task_name, source_name
            )
            SELECT * FROM freshness
            UNION ALL
            SELECT * FROM ocr
            UNION ALL
            SELECT * FROM search_queue
            UNION ALL
            SELECT * FROM runs
            ORDER BY section, metric
            LIMIT %(limit)s
            """,
            {"limit": limit},
        ).fetchall()
    emit(rows, {"row_count": len(rows), "read_only": True})


def factor_lifecycle_arg(value: Any, include_candidate: bool = False) -> list[str]:
    allowed = {"candidate", "shadow", "production", "retired"}
    values = [str(item).strip().lower() for item in (value or []) if str(item).strip()]
    values = [item for item in values if item in allowed]
    if not values:
        values = ["shadow", "production"]
        if include_candidate:
            values.insert(0, "candidate")
    return values


def run_factor_catalog(root: str, spec: dict[str, Any]) -> None:
    factor_name = str(spec.get("factor_name") or "").strip()
    family = str(spec.get("family") or "").strip().lower()
    include_candidate = bool(spec.get("include_candidate", False))
    lifecycles = factor_lifecycle_arg(spec.get("lifecycle_status"), include_candidate)
    limit = clamp_int(spec.get("limit"), 50, 1, 100)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH selected AS (
              SELECT * FROM factor.definitions
              WHERE lifecycle_status=ANY(%(lifecycles)s)
                AND (%(factor_name)s='' OR factor_name ILIKE '%%' || %(factor_name)s || '%%'
                     OR display_name ILIKE '%%' || %(factor_name)s || '%%')
                AND (%(family)s='' OR family=%(family)s)
              ORDER BY CASE lifecycle_status WHEN 'production' THEN 0 WHEN 'shadow' THEN 1 ELSE 2 END,
                       family,factor_name
              LIMIT %(limit)s
            ), score_stats AS (
              SELECT h.factor_id,min(h.dt) AS score_start_date,max(h.dt) AS score_end_date,
                     sum(h.score_rows)::bigint AS score_rows,
                     (array_agg(h.scored_assets ORDER BY h.dt DESC))[1] AS scored_assets,
                     (array_agg(h.coverage ORDER BY h.dt DESC))[1] AS latest_score_coverage,
                     (array_agg(h.zero_value_ratio ORDER BY h.dt DESC))[1] AS latest_zero_value_ratio,
                     (array_agg(h.raw_stddev ORDER BY h.dt DESC))[1] AS latest_raw_stddev,
                     max(h.latest_available_at) AS latest_available_at,
                     max(h.computed_at) AS health_computed_at
              FROM factor.factor_health_daily h JOIN selected d USING(factor_id)
              GROUP BY h.factor_id
            ), latest_evaluation AS (
              SELECT * FROM (
                SELECT e.*,r.train_start,r.train_end,r.test_start,r.test_end,r.horizon_days,
                       r.status AS run_status,
                       row_number() OVER (PARTITION BY e.factor_id ORDER BY e.created_at DESC,e.sample_name) AS rn
                FROM factor.evaluations e
                JOIN factor.experiment_runs r USING(run_id)
                JOIN selected d USING(factor_id)
                WHERE e.sample_name='test'
                  AND r.config->>'validation_state' IS DISTINCT FROM 'superseded'
              ) x WHERE rn=1
            )
            SELECT d.factor_name,d.display_name,d.family,d.description,d.direction,
                   d.required_features,d.lifecycle_status,d.version,d.promotion_gates,
                   ss.score_start_date,ss.score_end_date,ss.score_rows,ss.scored_assets,
                   ss.latest_score_coverage,ss.latest_zero_value_ratio,ss.latest_raw_stddev,
                   ss.latest_available_at,ss.health_computed_at,
                   le.sample_name AS validation_sample,le.train_start,le.train_end,
                   le.test_start,le.test_end,le.horizon_days,le.observation_count,
                   le.date_count,round(le.coverage::numeric,4) AS validation_coverage,
                   round(le.rank_ic_mean::numeric,5) AS rank_ic_mean,
                   round(le.rank_ic_ir::numeric,5) AS rank_ic_ir,
                   round(le.sign_consistency::numeric,4) AS sign_consistency,
                   round(le.top_bottom_spread_mean::numeric,5) AS top_bottom_spread_mean,
                   round(le.turnover_mean::numeric,5) AS turnover_mean,
                   round(le.p_value::numeric,6) AS p_value,
                   round(le.q_value::numeric,6) AS q_value,
                   le.passed_gates,le.run_status,le.created_at AS validation_created_at
            FROM selected d
            LEFT JOIN score_stats ss USING(factor_id)
            LEFT JOIN latest_evaluation le USING(factor_id)
            ORDER BY CASE d.lifecycle_status WHEN 'production' THEN 0 WHEN 'shadow' THEN 1 ELSE 2 END,
                     d.family,d.factor_name
            """,
            {
                "factor_name": factor_name, "family": family,
                "lifecycles": lifecycles, "limit": limit,
            },
        ).fetchall()
        summary = conn.execute(
            """SELECT lifecycle_status,count(*)::int AS factor_count
               FROM factor.definitions GROUP BY lifecycle_status ORDER BY lifecycle_status"""
        ).fetchall()
    production_count = next((int(row["factor_count"]) for row in summary if row["lifecycle_status"] == "production"), 0)
    emit(rows, {
        "factor_name": factor_name or None, "family": family or None,
        "lifecycle_status": lifecycles, "include_candidate": include_candidate,
        "row_count": len(rows), "lifecycle_summary": summary,
        "production_factor_count": production_count, "read_only": True,
        "coverage_source": "factor.factor_health_daily",
        "score_semantics": "raw_value is direction-adjusted; percentile/zscore are same-date cross-sectional scores, not return probabilities",
        "warning": "No production factors are available; shadow/candidate results are experimental."
                   if production_count == 0 else None,
    })


def run_factor_profile(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    as_of_date = str(spec.get("as_of_date") or "").strip()
    factor_names = [str(item).strip() for item in (spec.get("factor_names") or []) if str(item).strip()]
    include_candidate = bool(spec.get("include_candidate", False))
    lifecycles = factor_lifecycle_arg(spec.get("lifecycle_status"), include_candidate)
    lookback_days = clamp_int(spec.get("lookback_days"), 120, 10, 1095)
    limit = clamp_int(spec.get("limit"), 50, 1, 100)
    if not (stock_code or stock_name):
        emit([], {"row_count": 0, "read_only": True, "error": "stock_code or stock_name is required"})
        return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH assets AS (
              SELECT a.asset_id,a.symbol,a.name,a.industry_name
              FROM market.assets a
              WHERE a.asset_type='stock'
                AND (%(stock_code)s='' OR a.symbol=%(stock_code)s)
                AND (%(stock_name)s='' OR a.name ILIKE '%%' || %(stock_name)s || '%%')
            ), selected_date AS (
              SELECT max(h.dt) AS dt
              FROM factor.factor_health_daily h
              JOIN factor.definitions d USING(factor_id)
              WHERE d.lifecycle_status=ANY(%(lifecycles)s)
                AND (%(has_factor_names)s=false OR d.factor_name=ANY(%(factor_names)s))
                AND (%(as_of_date)s::date IS NULL OR h.dt<=%(as_of_date)s::date)
                AND (%(as_of_date)s::date IS NULL OR
                     h.latest_available_at <= (%(as_of_date)s::date::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai')
            ), history AS (
              SELECT s.*,d.factor_name,d.display_name,d.family,d.description,d.lifecycle_status,d.direction,
                     a.symbol AS stock_code,a.name AS stock_name,a.industry_name,
                     row_number() OVER (PARTITION BY s.factor_id,a.asset_id ORDER BY s.dt DESC) AS recency,
                     lead(s.percentile,5) OVER (PARTITION BY s.factor_id,a.asset_id ORDER BY s.dt DESC) AS percentile_5obs_ago,
                     lead(s.percentile,20) OVER (PARTITION BY s.factor_id,a.asset_id ORDER BY s.dt DESC) AS percentile_20obs_ago
              FROM factor.valid_daily_scores_v1 s
              JOIN factor.definitions d USING(factor_id)
              JOIN assets a USING(asset_id)
              CROSS JOIN selected_date sd
              WHERE s.dt BETWEEN sd.dt-%(lookback_days)s AND sd.dt
                AND d.lifecycle_status=ANY(%(lifecycles)s)
                AND (%(has_factor_names)s=false OR d.factor_name=ANY(%(factor_names)s))
            )
            SELECT stock_code,stock_name,industry_name,dt AS score_date,
                   factor_name,display_name,family,lifecycle_status,direction,
                   round(raw_value::numeric,6) AS raw_value,
                   round(percentile::numeric,6) AS percentile,
                   round(zscore::numeric,6) AS zscore,
                   round(percentile_5obs_ago::numeric,6) AS percentile_5obs_ago,
                   round((percentile-percentile_5obs_ago)::numeric,6) AS percentile_change_5obs,
                   round(percentile_20obs_ago::numeric,6) AS percentile_20obs_ago,
                   round((percentile-percentile_20obs_ago)::numeric,6) AS percentile_change_20obs,
                   source_feature_date,available_at,description
            FROM history WHERE recency=1
            ORDER BY percentile DESC NULLS LAST,factor_name
            LIMIT %(limit)s
            """,
            {
                "stock_code": stock_code, "stock_name": stock_name,
                "as_of_date": as_of_date or None, "lookback_days": lookback_days,
                "lifecycles": lifecycles, "has_factor_names": bool(factor_names),
                "factor_names": factor_names, "limit": limit,
            },
        ).fetchall()
        coverage = conn.execute(
            """SELECT min(dt) AS score_start_date,max(dt) AS score_end_date,
                      (array_agg(scored_assets ORDER BY dt DESC))[1] AS scored_assets
               FROM factor.factor_health_daily"""
        ).fetchone() or {}
    emit(rows, {
        "stock_code": stock_code or None, "stock_name": stock_name or None,
        "as_of_date_requested": as_of_date or None, "factor_names": factor_names,
        "lifecycle_status": lifecycles, "lookback_days": lookback_days,
        "row_count": len(rows), "read_only": True, **coverage,
        "score_semantics": "higher percentile means stronger in the factor-defined direction within that score date; it is not an up probability",
        "instruction": "Disclose score_date, available_at, lifecycle_status and score coverage. Candidate/shadow factors are experimental.",
    })


def run_factor_validation(root: str, spec: dict[str, Any]) -> None:
    factor_name = str(spec.get("factor_name") or "").strip()
    family = str(spec.get("family") or "").strip().lower()
    sample_name = str(spec.get("sample_name") or "").strip()
    passed_only = bool(spec.get("passed_only", False))
    latest_only = bool(spec.get("latest_only", True))
    match_mode = str(spec.get("match_mode") or "exact").strip().lower()
    if match_mode not in {"exact", "contains"}:
        match_mode = "exact"
    horizons = [int(value) for value in (spec.get("horizons") or []) if str(value).isdigit()]
    requested_horizons = list(horizons)
    horizons = [value for value in horizons if value in {5,20,60}] if spec.get("horizons") else [5,20,60]
    if not horizons:
        emit([], {"row_count":0,"read_only":True,"horizons":[],"supported_horizons":[5,20,60],
                  "error":"No executable stock horizon requested; 1d is diagnostic only under T+1."})
        return
    limit = clamp_int(spec.get("limit"), 50, 1, 100)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH ranked AS (
              SELECT d.factor_name,d.display_name,d.family,d.lifecycle_status,d.direction,
                     e.sample_name,e.observation_count,e.date_count,e.coverage,
                     e.rank_ic_mean,e.rank_ic_stddev,e.rank_ic_ir,e.sign_consistency,
                     e.top_bottom_spread_mean,e.turnover_mean,e.p_value,e.q_value,
                     e.passed_gates,e.metrics,e.created_at,
                     r.run_id,r.run_type,r.train_start,r.train_end,r.test_start,r.test_end,
                     r.horizon_days,r.universe,r.config,r.code_version,r.status AS run_status,
                     row_number() OVER (
                       PARTITION BY d.factor_id,e.sample_name,r.horizon_days
                       ORDER BY e.created_at DESC
                     ) AS recency
              FROM factor.definitions d
              JOIN factor.evaluations e USING(factor_id)
              JOIN factor.experiment_runs r USING(run_id)
              WHERE (%(factor_name)s='' OR
                     (%(match_mode)s='exact' AND d.factor_name=%(factor_name)s) OR
                     (%(match_mode)s='contains' AND (
                       d.factor_name ILIKE '%%' || %(factor_name)s || '%%'
                       OR d.display_name ILIKE '%%' || %(factor_name)s || '%%')))
                AND (%(family)s='' OR d.family=%(family)s)
                AND (%(sample_name)s='' OR e.sample_name=%(sample_name)s)
                AND (%(passed_only)s=false OR e.passed_gates=true)
                AND (%(has_horizons)s=false OR r.horizon_days=ANY(%(horizons)s))
                AND r.config->>'validation_state' IS DISTINCT FROM 'superseded'
            )
            SELECT factor_name,display_name,family,lifecycle_status,direction,
                   sample_name,run_type,train_start,train_end,test_start,test_end,horizon_days,
                   observation_count,date_count,round(coverage::numeric,4) AS coverage,
                   round(rank_ic_mean::numeric,6) AS rank_ic_mean,
                   round(rank_ic_stddev::numeric,6) AS rank_ic_stddev,
                   round(rank_ic_ir::numeric,6) AS rank_ic_ir,
                   round(sign_consistency::numeric,4) AS sign_consistency,
                   round(top_bottom_spread_mean::numeric,6) AS top_bottom_spread_mean,
                   round(turnover_mean::numeric,6) AS turnover_mean,
                   round(p_value::numeric,6) AS p_value,round(q_value::numeric,6) AS q_value,
                   passed_gates,run_status,universe,config,code_version,created_at
            FROM ranked
            WHERE (%(latest_only)s=false OR recency=1)
            ORDER BY passed_gates DESC,q_value NULLS LAST,abs(rank_ic_mean) DESC NULLS LAST,created_at DESC
            LIMIT %(limit)s
            """,
            {
                "factor_name": factor_name, "family": family,
                "sample_name": sample_name, "passed_only": passed_only,
                "latest_only": latest_only, "match_mode": match_mode,
                "has_horizons": bool(horizons), "horizons": horizons, "limit": limit,
            },
        ).fetchall()
    production_count = sum(1 for row in rows if row.get("lifecycle_status") == "production")
    emit(rows, {
        "factor_name": factor_name or None, "family": family or None,
        "sample_name": sample_name or None, "passed_only": passed_only,
        "latest_only": latest_only, "match_mode": match_mode, "horizons": horizons,
        "supported_horizons":[5,20,60],"excluded_horizons":[h for h in requested_horizons if h not in (5,20,60)],
        "row_count": len(rows), "read_only": True,
        "production_results": production_count,
        "instruction": "Report out-of-sample test dates, observations, q_value and lifecycle. passed_gates alone does not establish production readiness or guaranteed returns.",
    })


def run_factor_readiness(root: str, spec: dict[str, Any]) -> None:
    factor_name = str(spec.get("factor_name") or "").strip()
    family = str(spec.get("family") or "").strip().lower()
    include_candidate = bool(spec.get("include_candidate", False))
    lifecycles = factor_lifecycle_arg(spec.get("lifecycle_status"), include_candidate)
    limit = clamp_int(spec.get("limit"), 50, 1, 100)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.analytics.factor_validation import VALIDATION_VERSION, independent_windows
    _, cutoff = v3_request_context(spec)
    cutoff = min(cutoff, dt.datetime.now(dt.timezone.utc))
    if spec.get("as_of_date"):
        requested_day = dt.date.fromisoformat(str(spec["as_of_date"])[:10])
        cutoff = min(cutoff, dt.datetime.combine(requested_day, dt.time.max, ZoneInfo("Asia/Shanghai")))
    decision_date = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH selected AS (
              SELECT * FROM factor.definitions
              WHERE lifecycle_status=ANY(%(lifecycles)s)
                AND (%(factor_name)s='' OR factor_name=%(factor_name)s)
                AND (%(family)s='' OR family=%(family)s)
            ), latest_health AS (
              SELECT DISTINCT ON (h.factor_id) h.*
              FROM factor.factor_health_daily h JOIN selected d USING(factor_id)
              WHERE h.dt<=%(decision_date)s AND h.latest_available_at<=%(cutoff)s AND h.computed_at<=%(cutoff)s
              ORDER BY h.factor_id,h.dt DESC
            ), latest_eval AS (
              SELECT DISTINCT ON (e.factor_id) e.*,r.test_start,r.test_end,r.horizon_days,
                     r.status AS run_status
              FROM factor.evaluations e
              JOIN factor.experiment_runs r USING(run_id)
              JOIN selected d USING(factor_id)
              WHERE e.sample_name='test' AND r.status='complete'
                AND r.horizon_days IN (5,20,60) AND e.created_at<=%(cutoff)s AND r.finished_at<=%(cutoff)s
                AND r.test_end<=%(decision_date)s
                AND coalesce(nullif(e.metrics->>'outcome_end','')::date,r.test_end)<=%(decision_date)s
                AND r.config->>'validation_state' IS DISTINCT FROM 'superseded'
              ORDER BY e.factor_id,e.created_at DESC
            ), latest_approval AS (
              SELECT DISTINCT ON (p.factor_id) p.*
              FROM factor.production_approvals p JOIN selected d USING(factor_id)
              WHERE p.created_at<=%(cutoff)s
              ORDER BY p.factor_id,p.created_at DESC
            ), market_date AS (
              SELECT max(dt) AS latest_market_date FROM market.canonical_bars_daily WHERE dt<=%(decision_date)s
            )
            SELECT d.factor_id,d.factor_name,d.display_name,d.family,d.lifecycle_status,d.version,
                   d.promotion_gates,h.dt AS health_date,m.latest_market_date,
                   round(h.coverage::numeric,4) AS latest_coverage,
                   round(h.zero_value_ratio::numeric,4) AS zero_value_ratio,
                   round(h.raw_stddev::numeric,6) AS raw_stddev,h.latest_available_at,
                   0 AS passing_oos_windows, e.metrics AS evaluation_metrics,e.metrics,e.run_status,
                   e.run_id AS latest_evaluation_run_id,e.test_start,e.test_end,e.horizon_days,
                   e.observation_count,e.date_count,round(e.rank_ic_mean::numeric,6) AS rank_ic_mean,
                   round(e.rank_ic_ir::numeric,6) AS rank_ic_ir,
                   round(e.top_bottom_spread_mean::numeric,6) AS top_bottom_spread_mean,
                   round(e.turnover_mean::numeric,6) AS turnover_mean,
                   round(e.q_value::numeric,6) AS q_value,e.passed_gates,
                   p.decision AS latest_approval_decision,p.approved_by,p.effective_date,
                   p.factor_version AS approval_factor_version,p.expires_at AS approval_expires_at,p.created_at AS approval_created_at
            FROM selected d CROSS JOIN market_date m
            LEFT JOIN latest_health h USING(factor_id)
            LEFT JOIN latest_eval e USING(factor_id)
            LEFT JOIN latest_approval p USING(factor_id)
            ORDER BY CASE d.lifecycle_status WHEN 'production' THEN 0 WHEN 'shadow' THEN 1 ELSE 2 END,
                     d.family,d.factor_name
            LIMIT %(limit)s
            """,
            {"factor_name": factor_name, "family": family, "lifecycles": lifecycles, "limit": limit, "cutoff":cutoff,"decision_date":decision_date},
        ).fetchall()
        evaluations = conn.execute("""SELECT e.*,r.test_start,r.test_end,r.horizon_days,r.status
            FROM factor.evaluations e JOIN factor.experiment_runs r USING(run_id)
            WHERE e.sample_name='test' AND e.factor_id=ANY(%(factor_ids)s)
              AND r.horizon_days IN (5,20,60)
              AND e.created_at<=%(cutoff)s AND r.finished_at<=%(cutoff)s
              AND r.test_end<=%(decision_date)s
              AND coalesce(nullif(e.metrics->>'outcome_end','')::date,r.test_end)<=%(decision_date)s
              AND r.config->>'validation_state' IS DISTINCT FROM 'superseded' """,
            {"factor_ids":[r["factor_id"] for r in rows],"cutoff":cutoff,"decision_date":decision_date}).fetchall() if rows else []
    for row in rows:
        matching = [e for e in evaluations if e["factor_id"] == row["factor_id"]
                    and e["horizon_days"] == row.get("horizon_days")]
        row["passing_oos_windows"] = len(independent_windows(matching, row["version"]))
        row["current_validation_passed"] = bool(independent_windows([row], row["version"]))
        row["approval_valid_at_cutoff"] = bool(
            row.get("latest_approval_decision") == "approved"
            and str(row.get("approved_by") or "").strip()
            and row.get("approval_factor_version") == row["version"]
            and row.get("approval_created_at") is not None and row["approval_created_at"] <= cutoff
            and row.get("effective_date") is not None and row["effective_date"] <= decision_date
            and (row.get("approval_expires_at") is None or row["approval_expires_at"] > cutoff))
        checks = {
            "not_production": row.get("lifecycle_status") == "production",
            "fewer_than_3_independent_passing_oos_windows": row["passing_oos_windows"] >= 3,
            "latest_oos_current_contract_not_passed": row["current_validation_passed"],
            "unsupported_stock_horizon": row.get("horizon_days") in (5,20,60),
            "stale_or_missing_factor_health": row.get("health_date") is not None and row.get("health_date") == row.get("latest_market_date"),
            "coverage_below_80pct": float(row.get("latest_coverage") or 0) >= .80,
            "zero_cross_sectional_variance": row.get("raw_stddev") is not None and math.isfinite(float(row["raw_stddev"])) and float(row["raw_stddev"]) > 0,
            "factor_at_least_99pct_zero": row.get("zero_value_ratio") is not None and float(row["zero_value_ratio"]) < .99,
            "turnover_not_measured": row.get("turnover_mean") is not None,
            "nonpositive_top_bottom_spread": float(row.get("top_bottom_spread_mean") or 0) > 0,
            "missing_current_human_approval": row["approval_valid_at_cutoff"],
        }
        row["blockers"] = [reason for reason, passed in checks.items() if not passed]
        row["production_ready"] = not row["blockers"]
        row["prediction_eligible"] = row["production_ready"]
        row["required_validation_version"] = VALIDATION_VERSION
    emit(rows, {
        "factor_name": factor_name or None, "family": family or None,
        "lifecycle_status": lifecycles, "row_count": len(rows), "read_only": True,
        "minimum_independent_oos_windows": 3, "decision_cutoff":cutoff,
        "required_validation_version":VALIDATION_VERSION,"supported_horizons":[5,20,60],
        "registry_semantics":"Current factor registry; historical evidence and approvals are filtered at cutoff.",
        "instruction": "Only prediction_eligible=true may support production predictive claims. Readiness is governance evidence, not a return guarantee.",
    })


def run_factor_attribution(root: str, spec: dict[str, Any]) -> None:
    stock_code = str(spec.get("stock_code") or "").strip()
    stock_name = str(spec.get("stock_name") or "").strip()
    as_of_date = str(spec.get("as_of_date") or "").strip()
    factor_names = [str(item).strip() for item in (spec.get("factor_names") or []) if str(item).strip()]
    include_candidate = bool(spec.get("include_candidate", False))
    lifecycles = factor_lifecycle_arg(spec.get("lifecycle_status"), include_candidate)
    limit = clamp_int(spec.get("limit"), 30, 1, 62)
    if not (stock_code or stock_name):
        emit([], {"row_count": 0, "read_only": True, "error": "stock_code or stock_name is required"})
        return
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts.analytics.factor_validation import VALIDATION_VERSION, independent_windows
    _, cutoff = v3_request_context(spec)
    cutoff = min(cutoff, dt.datetime.now(dt.timezone.utc))
    if spec.get("as_of_date"):
        requested_day = dt.date.fromisoformat(str(spec["as_of_date"])[:10])
        cutoff = min(cutoff, dt.datetime.combine(requested_day, dt.time.max, ZoneInfo("Asia/Shanghai")))
    decision_date = cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        score_date = conn.execute("""SELECT max(h.dt) AS dt FROM factor.factor_health_daily h
            JOIN factor.definitions d USING(factor_id)
            WHERE d.lifecycle_status=ANY(%(lifecycles)s) AND h.dt<=%(decision_date)s
              AND h.latest_available_at<=%(cutoff)s AND h.computed_at<=%(cutoff)s""",
            {"lifecycles":lifecycles,"decision_date":decision_date,"cutoff":cutoff}).fetchone()["dt"]
        # Bind a concrete day so valid_daily_scores_v1 can prune its full-cohort
        # version aggregation; a joined max-date CTE can scan all history.
        rows = conn.execute(
            """
            WITH asset AS (
              SELECT asset_id,symbol,name,industry_name FROM market.assets
              WHERE asset_type='stock'
                AND (%(stock_code)s='' OR symbol=%(stock_code)s)
                AND (%(stock_name)s='' OR name ILIKE '%%' || %(stock_name)s || '%%')
              ORDER BY CASE WHEN symbol=%(stock_code)s THEN 0 ELSE 1 END,length(name) LIMIT 1
            ), latest_eval AS (
              SELECT DISTINCT ON (e.factor_id) e.*,r.test_start,r.test_end,r.horizon_days,r.status AS run_status
              FROM factor.evaluations e JOIN factor.experiment_runs r USING(run_id)
              WHERE e.sample_name='test' AND r.status='complete'
                AND r.horizon_days IN (5,20,60) AND e.created_at<=%(cutoff)s AND r.finished_at<=%(cutoff)s
                AND r.test_end<=%(decision_date)s
                AND coalesce(nullif(e.metrics->>'outcome_end','')::date,r.test_end)<=%(decision_date)s
                AND r.config->>'validation_state' IS DISTINCT FROM 'superseded'
              ORDER BY e.factor_id,e.created_at DESC
            )
            SELECT d.factor_id,d.version,e.metrics,e.run_status,
                   h.dt AS health_date,h.raw_stddev,h.coverage AS latest_coverage,
                   (SELECT max(dt) FROM market.canonical_bars_daily WHERE dt<=%(decision_date)s) AS latest_market_date,
                   p.decision AS latest_approval_decision,p.approved_by,p.effective_date,
                   p.factor_version AS approval_factor_version,p.expires_at AS approval_expires_at,p.created_at AS approval_created_at,
                   a.symbol AS stock_code,a.name AS stock_name,a.industry_name,
                   s.dt AS score_date,s.available_at,d.factor_name,d.display_name,d.family,
                   d.lifecycle_status,round(s.raw_value::numeric,6) AS raw_value,
                   round(s.percentile::numeric,6) AS percentile,round(s.zscore::numeric,6) AS zscore,
                   round(h.coverage::numeric,4) AS score_coverage,
                   round(h.zero_value_ratio::numeric,4) AS zero_value_ratio,
                   e.test_start,e.test_end,e.horizon_days,e.observation_count,
                   round(e.rank_ic_mean::numeric,6) AS rank_ic_mean,
                   round(e.rank_ic_ir::numeric,6) AS rank_ic_ir,
                   round(e.top_bottom_spread_mean::numeric,6) AS top_bottom_spread_mean,
                   round(e.turnover_mean::numeric,6) AS turnover_mean,
                   round(e.q_value::numeric,6) AS q_value,e.passed_gates
            FROM asset a
            JOIN factor.valid_daily_scores_v1 s ON s.asset_id=a.asset_id AND s.dt=%(score_date)s::date
            JOIN factor.definitions d USING(factor_id)
            LEFT JOIN factor.factor_health_daily h ON h.factor_id=s.factor_id AND h.dt=s.dt
            LEFT JOIN latest_eval e ON e.factor_id=s.factor_id
            LEFT JOIN LATERAL (SELECT * FROM factor.production_approvals p
              WHERE p.factor_id=d.factor_id AND p.created_at<=%(cutoff)s
              ORDER BY p.created_at DESC LIMIT 1) p ON true
            WHERE s.available_at<=%(cutoff)s AND s.source_decision_cutoff<=%(cutoff)s
              AND h.latest_available_at<=%(cutoff)s AND h.computed_at<=%(cutoff)s
              AND d.lifecycle_status=ANY(%(lifecycles)s)
              AND (%(has_factor_names)s=false OR d.factor_name=ANY(%(factor_names)s))
            ORDER BY abs(s.zscore) DESC NULLS LAST,s.percentile DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "stock_code": stock_code, "stock_name": stock_name, "as_of_date": as_of_date or None,
                "lifecycles": lifecycles, "has_factor_names": bool(factor_names),
                "factor_names": factor_names, "limit": limit, "cutoff":cutoff,"decision_date":decision_date,"score_date":score_date,
            },
        ).fetchall()
        evaluations = conn.execute("""SELECT e.*,r.test_start,r.test_end,r.horizon_days,r.status
            FROM factor.evaluations e JOIN factor.experiment_runs r USING(run_id)
            WHERE e.sample_name='test' AND e.factor_id=ANY(%(factor_ids)s)
              AND r.horizon_days IN (5,20,60)
              AND e.created_at<=%(cutoff)s AND r.finished_at<=%(cutoff)s
              AND r.test_end<=%(decision_date)s
              AND coalesce(nullif(e.metrics->>'outcome_end','')::date,r.test_end)<=%(decision_date)s
              AND r.config->>'validation_state' IS DISTINCT FROM 'superseded' """,
            {"factor_ids":[r["factor_id"] for r in rows],"cutoff":cutoff,"decision_date":decision_date}).fetchall() if rows else []
    for row in rows:
        matching = [e for e in evaluations if e["factor_id"] == row["factor_id"]
                    and e["horizon_days"] == row.get("horizon_days")]
        row["passing_oos_windows"] = len(independent_windows(matching, row["version"]))
        row["current_validation_passed"] = bool(independent_windows([row], row["version"]))
        row["approval_valid_at_cutoff"] = bool(
            row.get("latest_approval_decision") == "approved"
            and str(row.get("approved_by") or "").strip()
            and row.get("approval_factor_version") == row["version"]
            and row.get("approval_created_at") is not None and row["approval_created_at"] <= cutoff
            and row.get("effective_date") is not None and row["effective_date"] <= decision_date
            and (row.get("approval_expires_at") is None or row["approval_expires_at"] > cutoff))
        checks = {
            "not_production": row.get("lifecycle_status") == "production",
            "fewer_than_3_independent_passing_oos_windows": row["passing_oos_windows"] >= 3,
            "latest_oos_current_contract_not_passed": row["current_validation_passed"],
            "unsupported_stock_horizon": row.get("horizon_days") in (5,20,60),
            "stale_or_missing_factor_health": row.get("health_date") is not None and row.get("health_date") == row.get("latest_market_date"),
            "coverage_below_80pct": float(row.get("latest_coverage") or 0) >= .80,
            "zero_cross_sectional_variance": row.get("raw_stddev") is not None and math.isfinite(float(row["raw_stddev"])) and float(row["raw_stddev"]) > 0,
            "factor_at_least_99pct_zero": row.get("zero_value_ratio") is not None and float(row["zero_value_ratio"]) < .99,
            "turnover_not_measured": row.get("turnover_mean") is not None,
            "nonpositive_top_bottom_spread": float(row.get("top_bottom_spread_mean") or 0) > 0,
            "missing_current_human_approval": row["approval_valid_at_cutoff"],
        }
        row["blockers"] = [reason for reason, passed in checks.items() if not passed]
        row["production_ready"] = not row["blockers"]
        row["prediction_eligible"] = row["production_ready"]
        row["required_validation_version"] = VALIDATION_VERSION
    positive = [row["factor_name"] for row in rows if row.get("zscore") is not None and float(row["zscore"]) >= 1]
    negative = [row["factor_name"] for row in rows if row.get("zscore") is not None and float(row["zscore"]) <= -1]
    emit(rows, {
        "stock_code": stock_code or None, "stock_name": stock_name or None,
        "as_of_date_requested": as_of_date or None, "factor_names": factor_names,
        "lifecycle_status": lifecycles, "row_count": len(rows), "read_only": True,
        "positive_contributors": positive, "negative_contributors": negative,
        "prediction_eligible_count": sum(bool(row.get("prediction_eligible")) for row in rows),
        "decision_cutoff":cutoff,"required_validation_version":VALIDATION_VERSION,"minimum_independent_oos_windows":3,
        "supported_horizons":[5,20,60],"score_input_contract":"pit-input-v1",
        "instruction": "Attribution explains current cross-sectional signals and conflicts. It is not a forecast unless prediction_eligible is true.",
    })


def run_graph_paths(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("query") or "").strip()
    stock_code = str(spec.get("stock_code") or "").strip()
    target_query = str(spec.get("target_query") or "").strip()
    as_of_date = str(spec.get("as_of_date") or "").strip()
    known_at = str(spec.get("known_at") or "").strip()
    knowledge_mode = str(spec.get("knowledge_mode") or "public").strip().lower()
    if knowledge_mode not in {"public", "system"}:
        knowledge_mode = "public"
    direction = str(spec.get("direction") or "both").strip().lower()
    if direction not in {"both", "outgoing", "incoming"}:
        direction = "both"
    relation_types = [str(value).strip().upper() for value in (spec.get("relation_types") or []) if str(value).strip()]
    review_statuses = [str(value).strip().lower() for value in (spec.get("review_statuses") or []) if str(value).strip()]
    directness = [str(value).strip().lower() for value in (spec.get("directness") or []) if str(value).strip()]
    source_systems = [str(value).strip() for value in (spec.get("source_systems") or []) if str(value).strip()]
    require_evidence = bool(spec.get("require_evidence", False))
    exclude_event_relations = bool(spec.get("exclude_event_relations", False))
    max_depth = clamp_int(spec.get("max_depth"), 3, 1, 3)
    min_confidence = max(0.0, min(as_float(spec.get("min_confidence"), 0.5) or 0.0, 1.0))
    limit = clamp_int(spec.get("limit"), 20, 1, 50)
    if not (query or stock_code):
        emit([], {"row_count": 0, "read_only": True, "error": "query or stock_code is required"})
        return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH RECURSIVE params AS (
              SELECT coalesce(nullif(%(as_of_date)s,'')::date,current_date) AS as_of_date,
                     coalesce(nullif(%(known_at)s,'')::timestamptz,
                       (coalesce(nullif(%(as_of_date)s,'')::date,current_date)::timestamp+time '23:59:59')
                         AT TIME ZONE 'Asia/Shanghai') AS known_at
            ), seeds AS (
              SELECT e.entity_id,e.name,e.entity_type
              FROM kg.market_entities e
              WHERE (%(stock_code)s<>'' AND e.entity_type='company' AND e.aliases @> ARRAY[%(stock_code)s]::text[])
                 OR (%(query)s<>'' AND (
                      e.name ILIKE '%%' || %(query)s || '%%'
                      OR e.normalized_name ILIKE '%%' || lower(%(query)s) || '%%'
                      OR EXISTS (SELECT 1 FROM unnest(e.aliases) a WHERE a ILIKE '%%' || %(query)s || '%%')
                 ))
              ORDER BY CASE WHEN %(stock_code)s<>'' AND e.aliases @> ARRAY[%(stock_code)s]::text[] THEN 0 ELSE 1 END,
                       length(e.name),e.name
              LIMIT 5
            ), targets AS (
              SELECT e.entity_id
              FROM kg.market_entities e
              WHERE %(target_query)s<>'' AND (
                e.name ILIKE '%%' || %(target_query)s || '%%'
                OR e.normalized_name ILIKE '%%' || lower(%(target_query)s) || '%%'
                OR EXISTS (SELECT 1 FROM unnest(e.aliases) a WHERE a ILIKE '%%' || %(target_query)s || '%%')
              )
              LIMIT 20
            ), walk AS (
              SELECT 0 AS depth,s.entity_id AS current_id,
                     ARRAY[s.entity_id]::uuid[] AS entity_path,
                     ARRAY[]::uuid[] AS version_path,
                     1.0::double precision AS path_confidence
              FROM seeds s
              UNION ALL
              SELECT w.depth+1,edge.counterpart_id,w.entity_path||edge.counterpart_id,
                     w.version_path||edge.relation_version_id,
                     w.path_confidence*edge.confidence
              FROM walk w
              CROSS JOIN params p
              JOIN LATERAL (
                SELECT r.relation_id,r.source_entity_id,r.target_entity_id,r.relation_type,
                       v.relation_version_id,v.confidence,
                       CASE WHEN r.source_entity_id=w.current_id THEN r.target_entity_id ELSE r.source_entity_id END AS counterpart_id
                FROM kg.relations r JOIN kg.relation_versions v USING(relation_id)
                WHERE (r.source_entity_id=w.current_id OR r.target_entity_id=w.current_id)
                  AND (%(direction)s='both'
                       OR (%(direction)s='outgoing' AND r.source_entity_id=w.current_id)
                       OR (%(direction)s='incoming' AND r.target_entity_id=w.current_id))
                  AND (%(has_relation_types)s=false OR r.relation_type=ANY(%(relation_types)s::text[]))
                  AND (%(exclude_event_relations)s=false OR r.relation_type<>'AFFECTED_BY_EVENT')
                  AND v.status='active'
                  AND coalesce(v.attributes->'attribution_check'->>'supported','true')<>'false'
                  AND coalesce(v.attributes->>'superseded_by_canonical','false')<>'true'
                  AND v.confidence >= %(min_confidence)s
                  AND (%(has_review_statuses)s=false OR lower(v.review_status)=ANY(%(review_statuses)s::text[]))
                  AND (%(has_directness)s=false OR coalesce(lower(v.directness),'unknown')=ANY(%(directness)s::text[]))
                  AND (%(has_source_systems)s=false OR v.source_system=ANY(%(source_systems)s::text[]))
                  AND (%(require_evidence)s=false OR EXISTS (
                    SELECT 1 FROM kg.relation_version_evidence q
                    WHERE q.relation_version_id=v.relation_version_id AND q.system_at<=p.known_at AND q.public_at<=p.known_at
                      AND NOT EXISTS (SELECT 1 FROM corpus.document_quarantine quarantine
                        WHERE quarantine.document_id=q.document_id AND quarantine.released_at IS NULL)
                  ))
                  AND v.valid_during @> p.as_of_date
                  AND v.public_known_during @> p.known_at AND v.system_known_during @> p.known_at
                  AND CASE %(knowledge_mode)s
                        WHEN 'system' THEN v.system_known_during @> p.known_at
                        ELSE v.public_known_during @> p.known_at END
                ORDER BY v.confidence DESC,coalesce(v.weight,1) DESC,r.relation_type
                LIMIT 40
              ) edge ON true
              WHERE w.depth<%(max_depth)s AND NOT edge.counterpart_id=ANY(w.entity_path)
            ), selected_paths AS (
              SELECT w.* FROM walk w
              WHERE w.depth>0
                AND (%(target_query)s='' OR EXISTS (SELECT 1 FROM targets t WHERE t.entity_id=w.current_id))
              ORDER BY CASE WHEN %(target_query)s<>'' AND EXISTS (SELECT 1 FROM targets t WHERE t.entity_id=w.current_id) THEN 0 ELSE 1 END,
                       w.depth DESC,w.path_confidence DESC
              LIMIT %(limit)s
            )
            SELECT sp.depth,round(sp.path_confidence::numeric,6) AS path_confidence,
                   (SELECT jsonb_agg(jsonb_build_object(
                       'position',x.ord-1,'entity_id',e.entity_id,'name',e.name,
                       'entity_type',e.entity_type,'aliases',e.aliases
                     ) ORDER BY x.ord)
                    FROM unnest(sp.entity_path) WITH ORDINALITY x(entity_id,ord)
                    JOIN kg.market_entities e ON e.entity_id=x.entity_id) AS path_entities,
                   (SELECT jsonb_agg(jsonb_build_object(
                       'position',x.ord,'relation_id',r.relation_id,
                       'relation_version_id',v.relation_version_id,
                       'relation_type',r.relation_type,
                       'source_entity_id',r.source_entity_id,'target_entity_id',r.target_entity_id,
                       'valid_from',lower(v.valid_during),
                       'valid_to',CASE WHEN upper_inf(v.valid_during) THEN NULL ELSE upper(v.valid_during)-1 END,
                       'public_known_at',lower(v.public_known_during),
                       'system_known_at',lower(v.system_known_during),
                       'direction',v.direction,'confidence',round(v.confidence::numeric,4),
                       'weight',round(v.weight::numeric,4),'source_system',v.source_system,
                       'review_status',v.review_status,'directness',v.directness,
                       'extractor_version',v.extractor_version,
                       'evidence_document_id',ev.document_id,'evidence',left(ev.evidence_text,600),
                       'evidence_public_at',ev.public_at,'evidence_count',ev.evidence_count
                     ) ORDER BY x.ord)
                    FROM unnest(sp.version_path) WITH ORDINALITY x(version_id,ord)
                    JOIN kg.relation_versions v ON v.relation_version_id=x.version_id
                    JOIN kg.relations r USING(relation_id)
                    LEFT JOIN LATERAL (
                      SELECT e.document_id,e.evidence_text,e.public_at,count(*) OVER () AS evidence_count
                      FROM kg.relation_version_evidence e
                      WHERE e.relation_version_id=v.relation_version_id
                        AND e.system_at<=coalesce(nullif(%(known_at)s,'')::timestamptz,now())
                        AND e.public_at<=coalesce(nullif(%(known_at)s,'')::timestamptz,now())
                      ORDER BY e.confidence DESC NULLS LAST,e.created_at DESC LIMIT 1
                    ) ev ON true) AS path_edges
            FROM selected_paths sp
            ORDER BY sp.depth DESC,sp.path_confidence DESC
            """,
            {
                "query": query, "stock_code": stock_code, "target_query": target_query,
                "as_of_date": as_of_date, "known_at": known_at,
                "knowledge_mode": knowledge_mode, "direction": direction,
                "has_relation_types": bool(relation_types), "relation_types": relation_types,
                "exclude_event_relations": exclude_event_relations,
                "has_review_statuses": bool(review_statuses), "review_statuses": review_statuses,
                "has_directness": bool(directness), "directness": directness,
                "has_source_systems": bool(source_systems), "source_systems": source_systems,
                "require_evidence": require_evidence,
                "min_confidence": min_confidence, "max_depth": max_depth, "limit": limit,
            },
        ).fetchall()
        coverage = conn.execute(
            """
            WITH p AS (SELECT coalesce(nullif(%(as_of_date)s,'')::date,current_date) AS dt)
            SELECT max(c.dt) AS latest_coverage_date,
                   count(*)::int AS covered_source_days,
                   count(*) FILTER (WHERE c.status='complete')::int AS complete_source_days,
                   CASE WHEN sum(c.expected_documents)=0 THEN NULL
                        ELSE least(1.0,sum(greatest(c.rules_processed,c.llm_processed))::numeric/sum(c.expected_documents)) END AS coverage_score
            FROM kg.source_coverage_daily c CROSS JOIN p
            WHERE c.dt BETWEEN p.dt-29 AND p.dt
            """,
            {"as_of_date": as_of_date},
        ).fetchone() or {}
    emit(rows, {
        "query": query or None, "stock_code": stock_code or None,
        "target_query": target_query or None, "as_of_date": as_of_date or "current_date",
        "known_at": known_at or "end_of_as_of_date_Asia/Shanghai",
        "knowledge_mode": knowledge_mode, "direction": direction,
        "relation_types": relation_types, "max_depth": max_depth,
        "review_statuses": review_statuses, "directness": directness,
        "source_systems": source_systems, "require_evidence": require_evidence,
        "exclude_event_relations": exclude_event_relations,
        "min_confidence": min_confidence, "row_count": len(rows), "read_only": True,
        **coverage,
        "instruction": "Every path edge is an association, not causality. Treat unreviewed edges as candidates even when confidence is high; disclose review_status, directness, evidence_count, source, and coverage.",
    })


def run_knowledge_graph(root: str, spec: dict[str, Any]) -> None:
    query = str(spec.get("query") or "").strip()
    stock_code = str(spec.get("stock_code") or "").strip()
    relation_types = [str(value).strip().upper() for value in (spec.get("relation_types") or []) if str(value).strip()]
    review_statuses = [str(value).strip().lower() for value in (spec.get("review_statuses") or []) if str(value).strip()]
    directness = [str(value).strip().lower() for value in (spec.get("directness") or []) if str(value).strip()]
    source_systems = [str(value).strip() for value in (spec.get("source_systems") or []) if str(value).strip()]
    require_evidence = bool(spec.get("require_evidence", False))
    exclude_event_relations = bool(spec.get("exclude_event_relations", False))
    as_of_date = str(spec.get("as_of_date") or "").strip()
    start_date = str(spec.get("start_date") or "").strip()
    end_date = str(spec.get("end_date") or "").strip()
    known_at = str(spec.get("known_at") or "").strip()
    knowledge_mode = str(spec.get("knowledge_mode") or "public").strip().lower()
    query_mode = str(spec.get("query_mode") or "snapshot").strip().lower()
    if knowledge_mode not in {"public", "system"}:
        knowledge_mode = "public"
    if query_mode not in {"snapshot", "history", "changes", "neighbors"}:
        query_mode = "snapshot"
    lookback_days = clamp_int(spec.get("lookback_days"), 365, 1, 3650)
    limit = clamp_int(spec.get("limit"), 50, 1, 150)
    if not (query or stock_code):
        emit([], {"row_count": 0, "read_only": True, "error": "query or stock_code is required"})
        return
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH params AS (
              SELECT coalesce(nullif(%(as_of_date)s, '')::date,
                              nullif(%(end_date)s, '')::date,current_date) AS as_of_date,
                     coalesce(nullif(%(end_date)s, '')::date,
                              nullif(%(as_of_date)s, '')::date,current_date) AS end_date,
                     coalesce(nullif(%(start_date)s, '')::date,
                              coalesce(nullif(%(end_date)s, '')::date,
                                       nullif(%(as_of_date)s, '')::date,current_date)-(%(lookback_days)s::int-1)) AS start_date
            ), cutoffs AS (
              SELECT p.*,
                     coalesce(nullif(%(known_at)s,'')::timestamptz,
                       (p.end_date::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai') AS known_at
              FROM params p
            ), matched AS (
              SELECT e.*
              FROM kg.market_entities e
              WHERE (%(stock_code)s <> '' AND e.entity_type='company' AND e.aliases @> ARRAY[%(stock_code)s]::text[])
                 OR (%(query)s <> '' AND (
                      e.name ILIKE '%%' || %(query)s || '%%'
                      OR e.normalized_name ILIKE '%%' || lower(%(query)s) || '%%'
                      OR EXISTS (SELECT 1 FROM unnest(e.aliases) alias WHERE alias ILIKE '%%' || %(query)s || '%%')
                 ))
              ORDER BY CASE WHEN %(stock_code)s <> '' AND e.aliases @> ARRAY[%(stock_code)s]::text[] THEN 0 ELSE 1 END,
                       length(e.name), e.name
              LIMIT 20
            ), related AS (
              SELECT r.relation_id AS base_relation_id,r.relation_key,r.source_entity_id,r.target_entity_id,
                     r.relation_type,v.relation_version_id,v.valid_during,v.public_known_during,
                     v.system_known_during,v.direction,v.confidence,v.weight,v.status,
                     v.source_system,v.extractor_version,v.review_status,v.directness,v.attributes,
                     m.entity_id AS matched_entity_id,
                     CASE WHEN r.source_entity_id=m.entity_id THEN 'outgoing' ELSE 'incoming' END AS traversal_direction,
                     CASE WHEN r.source_entity_id=m.entity_id THEN r.target_entity_id ELSE r.source_entity_id END AS counterpart_id
              FROM matched m
              JOIN kg.relations r
                ON r.source_entity_id=m.entity_id OR r.target_entity_id=m.entity_id
              JOIN kg.relation_versions v ON v.relation_id=r.relation_id
              CROSS JOIN cutoffs p
              WHERE v.status NOT IN ('rejected','archived')
                AND CASE %(knowledge_mode)s
                      WHEN 'system' THEN v.system_known_during @> p.known_at
                      ELSE v.public_known_during @> p.known_at END
                AND CASE %(query_mode)s
                      WHEN 'snapshot' THEN v.valid_during @> p.as_of_date
                      WHEN 'neighbors' THEN v.valid_during @> p.as_of_date
                      WHEN 'changes' THEN lower(v.valid_during) BETWEEN p.start_date AND p.end_date
                      ELSE v.valid_during && daterange(p.start_date,p.end_date+1,'[)') END
                AND (%(has_relation_types)s=false OR r.relation_type=ANY(%(relation_types)s::text[]))
                AND (%(exclude_event_relations)s=false OR r.relation_type<>'AFFECTED_BY_EVENT')
                AND (%(has_review_statuses)s=false OR lower(v.review_status)=ANY(%(review_statuses)s::text[]))
                AND (%(has_directness)s=false OR coalesce(lower(v.directness),'unknown')=ANY(%(directness)s::text[]))
                AND (%(has_source_systems)s=false OR v.source_system=ANY(%(source_systems)s::text[]))
                AND (%(require_evidence)s=false OR EXISTS (
                  SELECT 1 FROM kg.relation_version_evidence q
                  WHERE q.relation_version_id=v.relation_version_id
                ))
            )
            SELECT r.base_relation_id AS relation_id, r.relation_version_id, r.relation_type, r.traversal_direction AS direction,
                   matched.entity_type AS subject_type, matched.name AS subject_name,
                   matched.aliases AS subject_aliases,
                   counterpart.entity_type AS counterpart_type, counterpart.name AS counterpart_name,
                   counterpart.aliases AS counterpart_aliases,
                   lower(r.valid_during) AS valid_from,
                   CASE WHEN upper_inf(r.valid_during) THEN NULL ELSE upper(r.valid_during)-1 END AS valid_to,
                   lower(r.public_known_during) AS public_known_at,
                   lower(r.system_known_during) AS system_known_at,
                   r.confidence, r.weight, r.direction AS numeric_direction,
                   r.review_status,r.directness,
                   evidence.document_id AS evidence_document_id,
                   left(evidence.evidence_text,600) AS evidence,
                   evidence.public_at AS evidence_public_at,evidence.evidence_count,
                   r.source_system, r.extractor_version, r.attributes
            FROM related r
            JOIN kg.market_entities matched ON matched.entity_id=r.matched_entity_id
            JOIN kg.market_entities counterpart ON counterpart.entity_id=r.counterpart_id
            LEFT JOIN LATERAL (
              SELECT e.document_id,e.evidence_text,e.public_at,count(*) OVER () AS evidence_count
              FROM kg.relation_version_evidence e
              WHERE e.relation_version_id=r.relation_version_id
              ORDER BY e.confidence DESC NULLS LAST,e.created_at DESC LIMIT 1
            ) evidence ON true
            ORDER BY lower(r.valid_during) DESC,r.confidence DESC NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "query": query, "stock_code": stock_code, "as_of_date": as_of_date,
                "start_date": start_date, "end_date": end_date, "known_at": known_at,
                "knowledge_mode": knowledge_mode, "query_mode": query_mode,
                "lookback_days": lookback_days, "has_relation_types": bool(relation_types),
                "relation_types": relation_types,
                "exclude_event_relations": exclude_event_relations,
                "has_review_statuses": bool(review_statuses), "review_statuses": review_statuses,
                "has_directness": bool(directness), "directness": directness,
                "has_source_systems": bool(source_systems), "source_systems": source_systems,
                "require_evidence": require_evidence, "limit": limit,
            },
        ).fetchall()
        coverage = conn.execute(
            """
            WITH p AS (
              SELECT coalesce(nullif(%(end_date)s,'')::date,nullif(%(as_of_date)s,'')::date,current_date) AS end_date,
                     coalesce(nullif(%(start_date)s,'')::date,
                       coalesce(nullif(%(end_date)s,'')::date,nullif(%(as_of_date)s,'')::date,current_date)-(%(lookback_days)s::int-1)) AS start_date
            )
            SELECT max(dt) AS latest_stats_date,
                   sum(relation_count)::bigint AS active_relation_count,
                   count(*)::int AS relation_group_count,
                   CASE WHEN sum(expected_documents)=0 THEN NULL
                        ELSE least(1.0,sum(greatest(rules_processed,llm_processed))::numeric/sum(expected_documents)) END AS coverage_score,
                   count(*) FILTER (WHERE status='complete') AS complete_source_days,
                   count(*) AS covered_source_days
            FROM kg.source_coverage_daily CROSS JOIN p
            WHERE dt BETWEEN p.start_date AND p.end_date
            """,
            {"start_date": start_date,"end_date": end_date,"as_of_date": as_of_date,"lookback_days": lookback_days},
        ).fetchone() or {}
    emit(rows, {
        "query": query, "stock_code": stock_code, "relation_types": relation_types,
        "as_of_date": as_of_date or None, "start_date": start_date or None,
        "end_date": end_date or None, "known_at": known_at or None,
        "knowledge_mode": knowledge_mode, "query_mode": query_mode, "lookback_days": lookback_days,
        "review_statuses": review_statuses, "directness": directness,
        "source_systems": source_systems, "require_evidence": require_evidence,
        "exclude_event_relations": exclude_event_relations,
        "row_count": len(rows), "latest_stats_date": coverage.get("latest_stats_date"),
        "active_relation_count": coverage.get("active_relation_count"),
        "relation_group_count": coverage.get("relation_group_count"),
        "coverage_score": coverage.get("coverage_score"),
        "complete_source_days": coverage.get("complete_source_days"),
        "covered_source_days": coverage.get("covered_source_days"),
        "temporal_semantics": "valid dates describe world state; known_at filters public/system knowledge state",
        "quality_semantics": "unreviewed relations are candidates, not verified facts, regardless of confidence",
        "read_only": True,
    })


def run_event_calendar(root: str, spec: dict[str, Any]) -> None:
    today = dt.date.today()
    start_date = str(spec.get("start_date") or today.isoformat())
    end_date = str(spec.get("end_date") or (today + dt.timedelta(days=30)).isoformat())
    stock_code = str(spec.get("stock_code") or "").strip()
    event_types = [str(value).strip() for value in (spec.get("event_types") or []) if str(value).strip()]
    include_reports = bool(spec.get("include_reports", True))
    limit = clamp_int(spec.get("limit"), 100, 1, 200)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH calendar AS (
              SELECT e.event_date, 'document_event'::text AS source_type, e.event_type,
                     code.stock_code, a.name AS stock_name, e.summary AS title,
                     left(e.evidence, 500) AS evidence, e.document_id, e.confidence,
                     e.metadata
              FROM analytics.document_events e
              LEFT JOIN LATERAL unnest(e.stock_codes) code(stock_code) ON true
              LEFT JOIN market.assets a ON a.symbol=code.stock_code AND a.asset_type='stock'
              WHERE e.event_date BETWEEN %(start_date)s::date AND %(end_date)s::date
                AND (%(stock_code)s='' OR code.stock_code=%(stock_code)s)
                AND (%(has_types)s=false OR e.event_type=ANY(%(event_types)s::text[]))
                AND e.extraction_method <> 'rules:v1'
              UNION ALL
              SELECT r.report_date, 'research_report', 'research_report', r.stock_code, r.stock_name,
                     r.report_title, concat_ws(' / ', r.institution, r.rating, r.rating_change),
                     r.document_id, NULL::double precision,
                     jsonb_build_object('institution', r.institution, 'rating', r.rating, 'rating_change', r.rating_change)
              FROM research.reports_resolved r
              WHERE %(include_reports)s
                AND r.report_date BETWEEN %(start_date)s::date AND %(end_date)s::date
                AND (%(stock_code)s='' OR r.stock_code=%(stock_code)s)
                AND (%(has_types)s=false OR 'research_report'=ANY(%(event_types)s::text[]))
              UNION ALL
              SELECT m.event_date, 'market_event', m.category, a.symbol, a.name,
                     m.category || ' / ' || coalesce(a.name, a.symbol),
                     left(m.raw_payload::text, 500), NULL::uuid, NULL::double precision,
                     jsonb_build_object('source', m.source, 'market_event_id', m.id)
              FROM market.events_raw m
              LEFT JOIN market.assets a ON a.asset_id=m.asset_id
              WHERE m.event_date BETWEEN %(start_date)s::date AND %(end_date)s::date
                AND (%(stock_code)s='' OR a.symbol=%(stock_code)s)
                AND (%(has_types)s=false OR m.category=ANY(%(event_types)s::text[]))
            )
            SELECT * FROM calendar
            ORDER BY event_date, source_type, stock_code NULLS LAST
            LIMIT %(limit)s
            """,
            {
                "start_date": start_date, "end_date": end_date, "stock_code": stock_code,
                "has_types": bool(event_types), "event_types": event_types,
                "include_reports": include_reports, "limit": limit,
            },
        ).fetchall()
    emit(rows, {
        "start_date": start_date, "end_date": end_date, "stock_code": stock_code or None,
        "event_types": event_types, "include_reports": include_reports,
        "row_count": len(rows), "read_only": True,
        "quality_note": "rules:v1 document events are excluded because their stock-code extraction was noisy",
    })


def _load_holdings(conn, spec: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    holdings_as_of = dt.date.fromisoformat(str(spec["as_of_date"])) if spec.get("as_of_date") else dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    inline = spec.get("holdings") or []
    portfolio_name = str(spec.get("portfolio_name") or "").strip()
    if inline:
        holdings = []
        for item in inline[:50]:
            if not isinstance(item, dict):
                continue
            code = str(item.get("stock_code") or "").strip()
            weight = as_float(item.get("weight"))
            if code and weight is not None and weight >= 0:
                holdings.append({"stock_code": code, "weight": weight})
        source = {"holdings_source": "inline", "portfolio_name": None}
    elif portfolio_name:
        holdings = conn.execute(
            """
            WITH portfolio AS (
              SELECT portfolio_id FROM agent.portfolios WHERE name=%s AND status='active' LIMIT 1
            ), latest AS (
              SELECT max(as_of_date) AS as_of_date FROM agent.portfolio_holdings
              WHERE portfolio_id=(SELECT portfolio_id FROM portfolio) AND as_of_date<=%s
            )
            SELECT stock_code, weight, market_value
            FROM agent.portfolio_holdings
            WHERE portfolio_id=(SELECT portfolio_id FROM portfolio)
              AND as_of_date=(SELECT as_of_date FROM latest)
            ORDER BY stock_code
            """,
            (portfolio_name, holdings_as_of),
        ).fetchall()
        source = {"holdings_source": "agent.portfolio_holdings", "portfolio_name": portfolio_name, "holdings_cutoff":str(holdings_as_of)}
    else:
        return [], {"holdings_source": None, "error": "holdings or portfolio_name is required"}
    if not holdings:
        return [], {**source, "error": "no valid holdings"}
    raw_weights = []
    for row in holdings:
        weight = as_float(row.get("weight"))
        if weight is None:
            weight = as_float(row.get("market_value"), 0) or 0
        raw_weights.append(max(0.0, weight))
    total = sum(raw_weights)
    if total <= 0:
        return [], {**source, "error": "holding weights or market values must sum to a positive number"}
    normalized = [
        {"stock_code": str(row["stock_code"]), "weight": raw_weights[index] / total}
        for index, row in enumerate(holdings)
    ]
    return normalized, {**source, "input_weight_sum": total, "weights_normalized": abs(total - 1.0) > 1e-9}


def run_portfolio_risk(root: str, spec: dict[str, Any]) -> None:
    as_of_text = str(spec.get("as_of_date") or "").strip()
    as_of = dt.date.fromisoformat(as_of_text) if as_of_text else dt.date.today()
    lookback_days = clamp_int(spec.get("lookback_days"), 252, 20, 1000)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        holdings, source_meta = _load_holdings(conn, spec)
        if not holdings:
            emit([], {**source_meta, "row_count": 0, "read_only": True})
            return
        codes = [row["stock_code"] for row in holdings]
        weights = {row["stock_code"]: row["weight"] for row in holdings}
        details = conn.execute(
            """
            SELECT DISTINCT ON (a.symbol) a.symbol AS stock_code, a.name AS stock_name,
                   i.industry_name
            FROM market.assets a
            LEFT JOIN LATERAL (SELECT industry_name FROM research.reports_resolved r WHERE r.stock_code=a.symbol
              AND r.available_at<=((%(as_of)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
              AND nullif(r.industry_name,'') IS NOT NULL ORDER BY available_at DESC,report_date DESC LIMIT 1) i ON true
            WHERE a.asset_type='stock' AND a.symbol=ANY(%(codes)s::text[])
            ORDER BY a.symbol, a.updated_at DESC
            """,
            {"codes": codes, "as_of": as_of},
        ).fetchall()
        daily = conn.execute(
            """
            WITH selected AS (
              SELECT DISTINCT ON (symbol) asset_id, symbol
              FROM market.assets WHERE asset_type='stock' AND symbol=ANY(%(codes)s::text[])
              ORDER BY symbol, updated_at DESC
            ), ranked AS (
              SELECT a.symbol AS stock_code, a.asset_id,
                     b.dt, b.close, 1 AS source_rank
              FROM selected a JOIN market.canonical_total_return_daily_v2 b ON b.asset_id=a.asset_id
              JOIN market.adjustment_factors_daily af ON af.asset_id=b.asset_id AND af.dt=b.dt
              WHERE b.dt BETWEEN %(since)s::date AND %(as_of)s::date
                AND b.quality_status='verified' AND af.quality_status='verified'
                AND greatest(b.available_at,af.available_at)<((%(as_of)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
            ), canonical AS (
              SELECT * FROM ranked WHERE source_rank=1
            ), calendar AS (
              SELECT trade_date,lag(trade_date) OVER (ORDER BY trade_date) previous_session
              FROM market.trading_calendar WHERE exchange='SSE' AND is_verified AND session_status='open'
            ), returns AS (
              SELECT stock_code, dt, close,
                     CASE WHEN lag(dt) OVER (PARTITION BY asset_id ORDER BY dt)=cal.previous_session
                       THEN close / nullif(lag(close) OVER (PARTITION BY asset_id ORDER BY dt), 0) - 1 END AS pct_chg_1d
              FROM canonical JOIN calendar cal ON cal.trade_date=canonical.dt
            )
            SELECT stock_code, dt, close, pct_chg_1d FROM returns
            WHERE pct_chg_1d IS NOT NULL ORDER BY dt, stock_code
            """,
            {"codes": codes, "since": as_of - dt.timedelta(days=lookback_days * 2 + 10), "as_of": as_of},
        ).fetchall()

    by_date: dict[Any, list[tuple[float, float]]] = {}
    for row in daily:
        if row["stock_code"] in weights:
            by_date.setdefault(row["dt"], []).append((weights[row["stock_code"]], float(row["pct_chg_1d"])))
    portfolio_returns = []
    for date, values in sorted(by_date.items())[-lookback_days:]:
        covered_weight = sum(weight for weight, _ in values)
        if covered_weight >= 1.0 - 1e-8:
            portfolio_returns.append((date, sum(weight * value for weight, value in values) / covered_weight, covered_weight))
    returns = [value for _, value, _ in portfolio_returns]
    cumulative = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        cumulative *= 1 + value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative / peak - 1)
    sorted_returns = sorted(returns)
    var_index = max(0, math.ceil(len(sorted_returns) * 0.05) - 1) if sorted_returns else 0
    industry_weights: dict[str, float] = {}
    stock_series: dict[str, list[dict[str, Any]]] = {}
    for item in daily:
        stock_series.setdefault(item["stock_code"], []).append(item)
    rows = []
    for row in details:
        code = row["stock_code"]
        weight = weights.get(code, 0)
        industry = row.get("industry_name") or "unknown"
        industry_weights[industry] = industry_weights.get(industry, 0) + weight
        series = stock_series.get(code, [])
        stock_returns = [float(item["pct_chg_1d"]) for item in series[-20:]]
        latest_close = float(series[-1]["close"]) if series else None
        ret_20d = (
            float(series[-1]["close"]) / float(series[-21]["close"]) - 1
            if len(series) >= 21 and float(series[-21]["close"]) else None
        )
        rows.append({
            **row, "weight": round(weight, 8),
            "latest_trade_date": series[-1]["dt"] if series else None,
            "close": latest_close, "ret_20d": ret_20d,
            "volatility_20d": statistics.stdev(stock_returns) if len(stock_returns) >= 2 else None,
        })
    annualized_volatility = statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 2 else None
    emit(rows, {
        **source_meta, "as_of_date": as_of, "lookback_days": lookback_days,
        "row_count": len(rows), "return_observations": len(returns),
        "first_return_date": portfolio_returns[0][0] if portfolio_returns else None,
        "latest_return_date": portfolio_returns[-1][0] if portfolio_returns else None,
        "portfolio_total_return": cumulative - 1 if returns else None,
        "annualized_volatility": annualized_volatility,
        "historical_var_95_daily": sorted_returns[var_index] if sorted_returns else None,
        "max_drawdown": max_drawdown if returns else None,
        "max_position_weight": max(weights.values()),
        "position_hhi": sum(weight * weight for weight in weights.values()),
        "industry_weights": dict(sorted(industry_weights.items(), key=lambda item: item[1], reverse=True)),
        "minimum_daily_covered_weight": min((covered for _, _, covered in portfolio_returns), default=None),
        "return_source": "canonical_total_return_daily_v2; verified corporate actions; as-of cutoff",
        "read_only": True,
        "risk_note": "Historical risk is descriptive, not a forecast. Only complete-weight daily observations are used; this is a fixed-weight risk proxy, not actual account NAV.",
    })


def run_watchlist_monitor(root: str, spec: dict[str, Any]) -> None:
    stock_codes = [str(value).strip() for value in (spec.get("stock_codes") or []) if str(value).strip()][:100]
    watchlist_name = str(spec.get("watchlist_name") or "").strip()
    days = clamp_int(spec.get("days"), 30, 1, 365)
    limit = clamp_int(spec.get("limit"), 100, 1, 150)
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(
            """
            WITH selected AS (
              SELECT unnest(%(stock_codes)s::text[]) AS stock_code, 'inline'::text AS source, NULL::text AS thesis, ARRAY[]::text[] AS tags, 3::smallint AS priority,
                     'user_requested'::text AS qualification_status,NULL::text AS last_qualified_date
              UNION ALL
              SELECT wi.stock_code, 'watchlist:' || w.name, wi.thesis, wi.tags, wi.priority,
                     coalesce(wi.metadata->>'qualification_status','未核实当期资格'),wi.metadata->>'last_qualified_date'
              FROM agent.watchlists w JOIN agent.watchlist_items wi USING (watchlist_id)
              WHERE %(watchlist_name)s <> '' AND w.name=%(watchlist_name)s AND w.status='active' AND wi.removed_at IS NULL
            ), dedup AS (
              SELECT DISTINCT ON (stock_code) * FROM selected ORDER BY stock_code, source
            )
            SELECT d.*, a.name AS stock_name, i.industry_name,
                   t.dt AS technical_date, t.close, t.pct_chg_1d, t.ret_5d, t.ret_20d,
                   s.composite_score, s.signal_tags,
                   coalesce(reports.report_count, 0) AS report_count,
                   reports.latest_report_date,
                   coalesce(events.event_count, 0) AS event_count,
                   events.latest_event_date
            FROM dedup d
            LEFT JOIN market.assets a ON a.symbol=d.stock_code AND a.asset_type='stock'
            LEFT JOIN analytics.asset_industry_current i ON i.asset_id=a.asset_id
            LEFT JOIN LATERAL (
              SELECT * FROM analytics.asset_technical_daily x WHERE x.asset_id=a.asset_id ORDER BY x.dt DESC LIMIT 1
            ) t ON true
            LEFT JOIN LATERAL (
              SELECT * FROM analytics.asset_signal_daily x WHERE x.asset_id=a.asset_id ORDER BY x.dt DESC LIMIT 1
            ) s ON true
            LEFT JOIN LATERAL (
              SELECT count(*)::int AS report_count, max(report_date) AS latest_report_date
              FROM research.reports_resolved r WHERE r.stock_code=d.stock_code AND r.report_date >= current_date-%(days)s
            ) reports ON true
            LEFT JOIN LATERAL (
              SELECT count(*)::int AS event_count, max(event_date) AS latest_event_date
              FROM analytics.document_events e
              WHERE e.stock_codes @> ARRAY[d.stock_code]::text[]
                AND e.event_date >= current_date-%(days)s AND e.extraction_method <> 'rules:v1'
            ) events ON true
            ORDER BY d.priority, d.stock_code
            LIMIT %(limit)s
            """,
            {"stock_codes": stock_codes, "watchlist_name": watchlist_name, "days": days, "limit": limit},
        ).fetchall()
    emit(rows, {
        "watchlist_name": watchlist_name or None, "inline_stock_count": len(stock_codes),
        "days": days, "row_count": len(rows), "read_only": True,
    })


def run_strategy_backtest(root: str, spec: dict[str, Any]) -> None:
    strategies = {"momentum_20d", "low_volatility"}
    strategy = str(spec.get("strategy") or "momentum_20d").strip().lower()
    if strategy not in strategies:
        emit([], {"row_count": 0, "error": "unsupported strategy", "allowed_strategies": sorted(strategies), "read_only": True})
        return
    end_date = dt.date.fromisoformat(str(spec.get("end_date") or dt.date.today().isoformat()))
    start_date = dt.date.fromisoformat(str(spec.get("start_date") or (end_date - dt.timedelta(days=365)).isoformat()))
    if start_date >= end_date or (end_date - start_date).days > 1095:
        emit([], {"row_count": 0, "error": "backtest window must be 1-1095 days", "read_only": True})
        return
    top_n = clamp_int(spec.get("top_n"), 20, 1, 50)
    rebalance_days = clamp_int(spec.get("rebalance_days"), 20, 5, 60)
    cost_bps = max(0.0, min(as_float(spec.get("transaction_cost_bps"), 10) or 0, 100))
    stock_codes = [str(value).strip() for value in (spec.get("stock_codes") or []) if str(value).strip()][:500]
    if not stock_codes:
        emit([], {
            "row_count": 0, "error": "an explicit stock_codes universe is required to bound cost and avoid implicit current-universe claims",
            "allowed_strategies": sorted(strategies), "read_only": True,
        })
        return
    score_sql = {
        "momentum_20d": "p.ret_20d",
        "low_volatility": "-p.volatility_20d",
    }[strategy]
    sql = f"""
      WITH selected_assets AS (
        SELECT DISTINCT ON (symbol) asset_id, symbol AS stock_code
        FROM market.assets WHERE asset_type='stock' AND symbol=ANY(%(stock_codes)s::text[])
        ORDER BY symbol, updated_at DESC
      ), ranked_prices AS (
        SELECT a.asset_id, a.stock_code, (b.ts AT TIME ZONE 'Asia/Shanghai')::date AS dt, b.close,
               row_number() OVER (
                 PARTITION BY b.asset_id, (b.ts AT TIME ZONE 'Asia/Shanghai')::date ORDER BY b.created_at DESC
               ) AS source_rank
        FROM selected_assets a JOIN market.bars_daily b ON b.asset_id=a.asset_id
        WHERE b.source='eastmoney:kline:day:qfq'
          AND b.ts >= ((%(start_date)s::date-60)::timestamp AT TIME ZONE 'Asia/Shanghai')
          AND b.ts < ((%(end_date)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
      ), canonical_prices AS (
        SELECT asset_id, stock_code, dt, close FROM ranked_prices WHERE source_rank=1
      ), price_features_1 AS (
        SELECT *, lag(close) OVER (PARTITION BY asset_id ORDER BY dt) AS close_1d,
                  lag(close,20) OVER (PARTITION BY asset_id ORDER BY dt) AS close_20d
        FROM canonical_prices
      ), price_features AS (
        SELECT *, close/nullif(close_1d,0)-1 AS pct_chg_1d,
                  close/nullif(close_20d,0)-1 AS ret_20d,
                  stddev_samp(close/nullif(close_1d,0)-1) OVER (
                    PARTITION BY asset_id ORDER BY dt ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                  ) AS volatility_20d
        FROM price_features_1
      ), dates AS (
        SELECT dt, row_number() OVER (ORDER BY dt) AS rn
        FROM (SELECT DISTINCT dt FROM price_features WHERE dt BETWEEN %(start_date)s AND %(end_date)s) x
      ), rebalance AS (
        SELECT dt AS rebalance_date, lead(dt, 1, %(end_date)s::date) OVER (ORDER BY dt) AS next_rebalance_date
        FROM dates WHERE rn=1 OR mod(rn-1, %(rebalance_days)s)=0
      ), candidates AS (
        SELECT rb.rebalance_date, rb.next_rebalance_date, p.asset_id, p.stock_code,
               {score_sql} AS score,
               row_number() OVER (PARTITION BY rb.rebalance_date ORDER BY {score_sql} DESC NULLS LAST, p.stock_code) AS rank
        FROM rebalance rb
        JOIN price_features p ON p.dt=rb.rebalance_date
        WHERE ({score_sql}) IS NOT NULL
      ), selected AS (
        SELECT * FROM candidates WHERE rank <= %(top_n)s
      ), portfolio_daily AS (
        SELECT d.dt, avg(coalesce(p.pct_chg_1d, 0)) AS gross_return,
               count(*) AS holding_count,
               count(p.pct_chg_1d) AS observed_holding_count,
               count(*)-count(p.pct_chg_1d) AS missing_holding_count,
               bool_or(d.dt=(SELECT min(d2.dt) FROM dates d2 WHERE d2.dt>sel.rebalance_date)) AS rebalance_day
        FROM selected sel
        JOIN dates d ON d.dt>sel.rebalance_date AND d.dt<=sel.next_rebalance_date
        LEFT JOIN price_features p ON p.asset_id=sel.asset_id AND p.dt=d.dt
        GROUP BY d.dt
      )
      SELECT dt, gross_return,
             gross_return - CASE WHEN rebalance_day THEN %(cost_rate)s ELSE 0 END AS net_return,
             holding_count, observed_holding_count, missing_holding_count, rebalance_day
      FROM portfolio_daily ORDER BY dt
    """
    with connect(root) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(sql, {
            "start_date": start_date, "end_date": end_date, "rebalance_days": rebalance_days,
            "stock_codes": stock_codes,
            "top_n": top_n, "cost_rate": cost_bps / 10000.0,
        }).fetchall()
    returns = [float(row["net_return"]) for row in rows if row.get("net_return") is not None]
    cumulative = 1.0
    peak = 1.0
    max_drawdown = 0.0
    wins = 0
    for value in returns:
        cumulative *= 1 + value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative / peak - 1)
        wins += value > 0
    years = max((end_date - start_date).days / 365.25, 1 / 365.25)
    annual_return = cumulative ** (1 / years) - 1 if returns and cumulative > 0 else None
    annual_vol = statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 2 else None
    incomplete_days = sum(1 for row in rows if int(row.get("missing_holding_count") or 0) > 0)
    emit(rows[-260:], {
        "strategy": strategy, "start_date": start_date, "end_date": end_date,
        "top_n": top_n, "rebalance_days": rebalance_days, "transaction_cost_bps": cost_bps,
        "universe": "explicit_stock_codes", "universe_count": len(stock_codes),
        "row_count": len(rows), "returned_daily_rows": min(len(rows), 260),
        "total_return": cumulative - 1 if returns else None, "annualized_return": annual_return,
        "annualized_volatility": annual_vol,
        "sharpe_zero_rf": annual_return / annual_vol if annual_return is not None and annual_vol else None,
        "max_drawdown": max_drawdown if returns else None,
        "win_rate_daily": wins / len(returns) if returns else None,
        "incomplete_price_days": incomplete_days,
        "missing_price_policy": "zero daily return for a selected holding without a canonical bar; missing counts are returned per day",
        "signal_timing": "signals observed at rebalance close; returns begin on the next trading day",
        "price_source": "canonical market.bars_daily eastmoney:kline:day:qfq Asia/Shanghai dates",
        "weighting": "equal_weight",
        "known_biases": [
            "the caller-supplied universe may contain survivorship or selection bias",
            "transaction cost assumes full one-way turnover at each rebalance and does not model market impact",
            "no benchmark, dividends, suspension execution, or limit-up/down fill model",
        ],
        "trusted_for": "research diagnostics only; not execution-grade performance claims",
        "read_only": True,
    })


def main() -> int:
    mode = sys.argv[1]
    root = sys.argv[2]
    timeout_by_mode = {
        "v3_time_context": 3000,
        "v3_data_status": 3000,
        "v3_claim_verify": 3000,
        "v3_market_series_batch": 8000,
        "v3_fundamentals_batch": 8000,
        "v3_factor_analysis": 8000,
        "v3_graph_analysis": 8000,
        "v3_research_bundle": 20000,
        "v3_research_intelligence": 8000,
        "v3_document_retrieve": 8000,
        "document_search": 8000,
        "document_bm25_search": 8000,
        "document_evidence": 8000,
        "research_workflow": 20000,
    }
    os.environ.setdefault(
        "FINANCE_DB_STATEMENT_TIMEOUT_MS", str(timeout_by_mode.get(mode, 15000))
    )
    if mode == "query":
        run_query(root, sys.argv[3], int(sys.argv[4]))
    elif mode == "schema":
        run_schema(
            root,
            sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None,
            sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None,
        )
    elif mode == "theme_map":
        run_theme_map(root, json_arg(3))
    elif mode == "price_movers":
        run_price_movers(root, json_arg(3))
    elif mode == "asset_snapshot":
        run_asset_snapshot(root, json_arg(3))
    elif mode == "fundamental_analysis":
        run_fundamental_analysis(root, json_arg(3))
    elif mode == "peer_compare":
        run_peer_compare(root, json_arg(3))
    elif mode == "relative_valuation":
        run_relative_valuation(root, json_arg(3))
    elif mode == "capital_flow_analysis":
        run_capital_flow_analysis(root, json_arg(3))
    elif mode == "macro_context":
        run_macro_context(root, json_arg(3))
    elif mode == "factor_catalog":
        run_factor_catalog(root, json_arg(3))
    elif mode == "factor_profile":
        run_factor_profile(root, json_arg(3))
    elif mode == "factor_validation":
        run_factor_validation(root, json_arg(3))
    elif mode == "factor_readiness":
        run_factor_readiness(root, json_arg(3))
    elif mode == "factor_attribution":
        run_factor_attribution(root, json_arg(3))
    elif mode == "entity_resolve":
        run_entity_resolve(root, json_arg(3))
    elif mode == "data_context":
        run_data_context(root, json_arg(3))
    elif mode == "market_screen":
        run_market_screen(root, json_arg(3))
    elif mode == "research_momentum":
        run_research_momentum(root, json_arg(3))
    elif mode == "screen_stocks":
        run_screen_stocks(root, json_arg(3))
    elif mode == "stock_profile":
        run_stock_profile(root, json_arg(3))
    elif mode == "document_search":
        run_document_search(root, json_arg(3))
    elif mode == "document_bm25_search":
        run_document_bm25_search(root, json_arg(3))
    elif mode == "topic_clusters":
        run_topic_clusters(root, json_arg(3))
    elif mode == "document_evidence":
        run_document_evidence(root, json_arg(3))
    elif mode == "subindustry_trends":
        run_subindustry_trends(root, json_arg(3))
    elif mode == "company_exposure":
        run_company_exposure(root, json_arg(3))
    elif mode == "forecast_trends":
        run_forecast_trends(root, json_arg(3))
    elif mode == "event_study":
        run_event_study(root, json_arg(3))
    elif mode == "financial_growth_screen":
        run_financial_growth_screen(root, json_arg(3))
    elif mode == "event_return":
        run_event_return(root, json_arg(3))
    elif mode == "fact_check_stock":
        run_fact_check_stock(root, json_arg(3))
    elif mode == "target_price_history":
        run_target_price_history(root, json_arg(3))
    elif mode == "price_series":
        run_price_series(root, json_arg(3))
    elif mode == "limit_up_list":
        run_limit_up_list(root, json_arg(3))
    elif mode == "data_health":
        run_data_health(root, json_arg(3))
    elif mode == "knowledge_graph":
        run_knowledge_graph(root, json_arg(3))
    elif mode == "graph_paths":
        run_graph_paths(root, json_arg(3))
    elif mode == "event_calendar":
        run_event_calendar(root, json_arg(3))
    elif mode == "portfolio_risk":
        run_portfolio_risk(root, json_arg(3))
    elif mode == "watchlist_monitor":
        run_watchlist_monitor(root, json_arg(3))
    elif mode == "strategy_backtest":
        run_strategy_backtest(root, json_arg(3))
    elif mode == "v3_time_context":
        run_v3_time_context(root, json_arg(3))
    elif mode == "v3_data_status":
        run_v3_data_status(root, json_arg(3))
    elif mode == "v3_market_series_batch":
        run_v3_market_series_batch(root, json_arg(3))
    elif mode == "v3_fundamentals_batch":
        run_v3_fundamentals_batch(root, json_arg(3))
    elif mode == "v3_factor_analysis":
        run_v3_factor_analysis(root, json_arg(3))
    elif mode == "v3_graph_analysis":
        run_v3_graph_analysis(root, json_arg(3))
    elif mode == "v3_research_intelligence":
        run_v3_research_intelligence(root, json_arg(3))
    elif mode == "v3_research_bundle":
        run_v3_research_bundle(root, json_arg(3))
    elif mode == "v3_document_retrieve":
        run_v3_document_retrieve(root, json_arg(3))
    elif mode == "v3_claim_verify":
        run_v3_claim_verify(root, json_arg(3))
    else:
        raise SystemExit(f"unknown mode: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
