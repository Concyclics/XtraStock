# -*- coding: utf-8 -*-
"""Extract AI hardware fine-grained term mentions from documents."""
from __future__ import annotations

import argparse
import hashlib
import re
from typing import Any

from ai_hardware_lexicon import AI_CONTEXT_TERMS
from common import connect, jsonb, parse_source_types
from company_evidence_scope import scope_evidence


STOCK_CODE_RE = re.compile(r"(?<!\d)(?:[0368]\d{5}|920\d{3})(?!\d)")


def norm_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def evidence_for(text: str, alias: str, window: int = 120) -> str:
    span = find_alias_span(text, alias)
    idx = span[0] if span else -1
    if idx < 0:
        return alias
    start = max(0, idx - window)
    end = min(len(text), idx + len(alias) + window)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def find_alias_span(text: str, alias: str) -> tuple[int, int] | None:
    if not alias:
        return None
    # ASCII abbreviations must match token boundaries. This avoids matching MPO in "Important".
    if re.fullmatch(r"[A-Za-z0-9.+-]{2,}", alias):
        m = re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, flags=re.I)
        return m.span() if m else None
    idx = text.lower().find(alias.lower())
    if idx < 0:
        return None
    return idx, idx + len(alias)


def mention_hash(document_id: str, term_id: str, evidence: str, method: str) -> str:
    raw = f"{document_id}|{term_id}|{evidence[:200]}|{method}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def relevance(base: float, text: str, context_hits: list[str]) -> float:
    score = float(base or 0)
    if context_hits:
        score += min(0.25, 0.06 * len(context_hits))
    if any(x in text for x in ("半导体", "芯片", "晶圆", "封装", "服务器", "数据中心", "算力")):
        score += 0.08
    return max(0.0, min(1.0, score))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-types", default="research_report,news,peoples_daily")
    parser.add_argument("--since", default="2026-06-01")
    parser.add_argument("--until")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--min-relevance", type=float, default=0.45)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--method", default="ai_hardware_rules:v1")
    args = parser.parse_args()

    source_types = parse_source_types(args.source_types)
    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT term_id::text, term, normalized_term, subindustry, category,
                       aliases, ai_hardware_relevance
                FROM analytics.ai_hardware_terms
                ORDER BY ai_hardware_relevance DESC, length(term) DESC
                """
            )
            terms = cur.fetchall()
            if not terms:
                raise SystemExit("analytics.ai_hardware_terms is empty; run seed_ai_hardware_terms.py first.")

            cur.execute(
                """
                SELECT
                  d.id::text AS document_id,
                  d.source_type,
                  d.source_name,
                  d.title,
                  COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) AS mention_date,
                  r.stock_code,
                  r.stock_name,
                  r.industry_name,
                  left(concat_ws(E'\n', d.title, d.content_text, d.content_md, d.raw_text), 20000) AS text
                FROM corpus.documents d
                LEFT JOIN research.reports_resolved r ON r.document_id = d.id
                WHERE (%(source_types)s::text[] IS NULL OR d.source_type = ANY(%(source_types)s))
                  AND (%(since)s::date IS NULL OR COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) >= %(since)s::date)
                  AND (%(until)s::date IS NULL OR COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) <= %(until)s::date)
                  AND length(concat_ws(E'\n', d.title, d.content_text, d.content_md, d.raw_text)) > 20
                  AND (
                    %(force)s
                    OR NOT EXISTS (
                      SELECT 1
                      FROM analytics.ai_hardware_mentions m
                      WHERE m.document_id = d.id
                        AND m.extraction_method = %(method)s
                    )
                  )
                ORDER BY COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) DESC NULLS LAST, d.id
                LIMIT %(limit)s
                """,
                {
                    "source_types": source_types,
                    "since": args.since,
                    "until": args.until,
                    "force": args.force,
                    "method": args.method,
                    "limit": args.limit,
                },
            )
            rows = cur.fetchall()
            inserted = 0
            matched_docs = 0
            for row in rows:
                text = norm_text(row.get("text"))
                if not text:
                    continue
                context_hits = [t for t in AI_CONTEXT_TERMS if t.lower() in text.lower()]
                base_stock_codes = set()
                stock_names = set()
                industries = set()
                if row.get("stock_code"):
                    base_stock_codes.add(str(row["stock_code"]))
                if row.get("stock_name"):
                    stock_names.add(str(row["stock_name"]))
                if row.get("industry_name"):
                    industries.add(str(row["industry_name"]))

                doc_hit = False
                for term in terms:
                    aliases = [term["term"], term["normalized_term"]] + list(term.get("aliases") or [])
                    hit_alias = None
                    for alias in sorted(set(x for x in aliases if x), key=len, reverse=True):
                        if find_alias_span(text, alias):
                            hit_alias = alias
                            break
                    if not hit_alias:
                        continue
                    score = relevance(term["ai_hardware_relevance"], text, context_hits)
                    if score < args.min_relevance:
                        continue
                    evidence = evidence_for(text, hit_alias)
                    stock_codes = set(base_stock_codes)
                    if not stock_codes:
                        stock_codes.update(STOCK_CODE_RE.findall(evidence))
                    stock_codes = {code for code in stock_codes if scope_evidence(
                        evidence,code,row.get('stock_name') if code==row.get('stock_code') else None,
                        [hit_alias],row.get('stock_code'))['supported']}
                    stock_names = {str(row['stock_name'])} if row.get('stock_code') in stock_codes and row.get('stock_name') else set()
                    cur.execute(
                        """
                        INSERT INTO analytics.ai_hardware_mentions (
                          term_id, document_id, mention_date, source_type, source_name,
                          stock_codes, stock_names, industries, evidence, ai_context_terms,
                          ai_hardware_relevance, extraction_method, mention_hash, metadata, updated_at
                        )
                        VALUES (
                          %(term_id)s, %(document_id)s, %(mention_date)s, %(source_type)s, %(source_name)s,
                          %(stock_codes)s, %(stock_names)s, %(industries)s, %(evidence)s, %(context_terms)s,
                          %(score)s, %(method)s, %(mention_hash)s, %(metadata)s, now()
                        )
                        ON CONFLICT (mention_hash) DO UPDATE SET
                          stock_codes = EXCLUDED.stock_codes,
                          stock_names = EXCLUDED.stock_names,
                          industries = EXCLUDED.industries,
                          evidence = EXCLUDED.evidence,
                          ai_context_terms = EXCLUDED.ai_context_terms,
                          ai_hardware_relevance = EXCLUDED.ai_hardware_relevance,
                          metadata = EXCLUDED.metadata,
                          updated_at = now()
                        """,
                        {
                            "term_id": term["term_id"],
                            "document_id": row["document_id"],
                            "mention_date": row["mention_date"],
                            "source_type": row["source_type"],
                            "source_name": row["source_name"],
                            "stock_codes": sorted(stock_codes),
                            "stock_names": sorted(stock_names),
                            "industries": sorted(industries | {term["subindustry"]}),
                            "evidence": evidence,
                            "context_terms": context_hits,
                            "score": score,
                            "method": args.method,
                            "mention_hash": mention_hash(row["document_id"], term["term_id"], evidence, args.method),
                            "metadata": jsonb(
                                {
                                    "matched_alias": hit_alias,
                                    "term": term["term"],
                                    "subindustry": term["subindustry"],
                                    "category": term["category"],
                                }
                            ),
                        },
                    )
                    inserted += 1
                    doc_hit = True
                if doc_hit:
                    matched_docs += 1
            conn.commit()
    print(f"scanned={len(rows)} matched_docs={matched_docs} mentions={inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
