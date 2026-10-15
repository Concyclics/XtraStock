# -*- coding: utf-8 -*-
"""Discover AI hardware concept and company exposure candidates from documents."""
from __future__ import annotations

import argparse
import re
from typing import Any

from ai_hardware_lexicon import AI_CONTEXT_TERMS
from common import connect, jsonb, parse_source_types
from company_evidence_scope import scope_evidence


STOCK_CODE_RE = re.compile(r"(?<!\d)(?:[0368]\d{5}|920\d{3})(?!\d)")
CLAUSE_SPLIT_RE = re.compile(r"[。；;！？\n]")
THIRD_PARTY_ORG_RE = re.compile(
    r"(SpaceX|Starcloud|Starlcoud|Google|Meta|Microsoft|OpenAI|英伟达|NVIDIA|微软|谷歌|蓝色起源|"
    r"台积电|博通|Marvell|AMD|亚马逊|AWS|特斯拉|特斯拉|三星|SK海力士|"
    r"摩尔线程|沐曦|壁仞|燧原|寒武纪|西安奕材)",
    re.I,
)
FINANCIAL_SERVICE_RE = re.compile(
    r"(保荐|承销|投行|IPO|并购|定增|再融资|私募基金|基金平台|资管|理财|贷款|授信|"
    r"融资支持|证券承销|银行授信|银行贷款|金融机构|券商业务)"
)

RELATION_PATTERNS = [
    ("material_supplier", re.compile(r"(材料|特气|靶材|树脂|铜箔|玻璃|镍粉|光刻胶|硅片|CMP)")),
    ("equipment_supplier", re.compile(r"(设备|检测|测试机|量检测|清洗|刻蚀|沉积|电镀|曝光|钻孔)")),
    ("direct_product", re.compile(r"(布局|推出|量产|出货|供应|研发|生产|覆盖|切入|导入|送样|配套)")),
    ("downstream_customer", re.compile(r"(采购|客户|需求方|云厂商|数据中心|服务器厂)")),
]

TERM_HINTS = [
    ("芯片设计", "算力芯片", "subsegment"),
    ("GPU", "算力芯片", "component"),
    ("ASIC", "算力芯片", "component"),
    ("EDA", "芯片设计", "component"),
    ("半导体设备", "半导体设备", "segment"),
    ("刻蚀", "半导体设备", "equipment"),
    ("薄膜沉积", "半导体设备", "equipment"),
    ("清洗设备", "半导体设备", "equipment"),
    ("测试机", "半导体设备", "equipment"),
    ("量检测", "半导体设备", "equipment"),
    ("半导体材料", "半导体材料", "segment"),
    ("光刻胶", "半导体材料", "material"),
    ("电子特气", "半导体材料", "material"),
    ("光模块", "光互连", "subsegment"),
    ("光芯片", "光互连", "component"),
    ("光纤", "光互连", "component"),
    ("MPO", "光纤连接器", "component"),
    ("CPO", "光互连", "subsegment"),
    ("LPO", "光模块", "subsegment"),
    ("PCB", "PCB与封装", "subsegment"),
    ("玻璃基板", "PCB与封装", "material"),
    ("ABF", "PCB与封装", "material"),
    ("HBM", "AI存储", "component"),
    ("MLCC", "电源与能源", "component"),
    ("超级电容", "电源与能源", "component"),
    ("服务器电源", "电源与能源", "component"),
    ("液冷", "散热", "component"),
]


def clean_text(value: Any) -> str:
    return "" if value is None else str(value)


def aliases_match(text: str, term: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9.+-]{2,}", term):
        return re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text, flags=re.I) is not None
    return term.lower() in text.lower()


def evidence_clause(text: str, term: str, window: int = 180) -> str:
    idx = text.lower().find(term.lower())
    if idx < 0:
        return term
    start = max(0, idx - window)
    end = min(len(text), idx + len(term) + window)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    return snippet[:900]


def infer_company_relation(evidence: str) -> str:
    if FINANCIAL_SERVICE_RE.search(evidence):
        return "weak_mention"
    for relation, pattern in RELATION_PATTERNS:
        if pattern.search(evidence):
            return relation
    return "weak_mention"


def report_stock_supported(doc: dict[str, Any], evidence: str, relation: str) -> bool:
    """Avoid attributing every thematic mention in a stock report to the report target."""
    stock_code = doc.get("stock_code")
    stock_name = clean_text(doc.get("stock_name"))
    if not stock_code:
        return False
    if stock_code in evidence or (stock_name and stock_name in evidence):
        return True
    if relation == "weak_mention":
        return False
    if THIRD_PARTY_ORG_RE.search(evidence):
        return False
    if FINANCIAL_SERVICE_RE.search(evidence):
        return False
    return bool(re.search(r"(产品|客户|订单|产能|出货|量产|布局|研发|生产|供应|导入|送样|配套|切入)", evidence))


def score_candidate(text: str, evidence: str, base: float = 0.55) -> float:
    score = base
    ctx = [x for x in AI_CONTEXT_TERMS if x.lower() in text.lower()]
    score += min(0.25, len(ctx) * 0.05)
    if any(x in evidence for x in ("AI服务器", "算力", "数据中心", "GPU", "HBM", "英伟达", "先进封装")):
        score += 0.18
    if infer_company_relation(evidence) != "weak_mention":
        score += 0.12
    return max(0, min(1, score))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-types", default="research_report,news")
    parser.add_argument("--since", default="2026-01-01")
    parser.add_argument("--until")
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    source_types = parse_source_types(args.source_types)
    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH recent_ids AS MATERIALIZED (
                  SELECT document_id AS id FROM research.reports
                  WHERE report_date BETWEEN coalesce(%(since)s::date,'1900-01-01'::date)
                                        AND coalesce(%(until)s::date,'9999-12-31'::date)
                  UNION
                  SELECT id FROM corpus.documents
                  WHERE published_at >= (coalesce(%(since)s::date,'1900-01-01'::date)::timestamp AT TIME ZONE 'Asia/Shanghai')
                    AND published_at < ((coalesce(%(until)s::date,'9999-12-30'::date)+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
                ), eligible AS MATERIALIZED (
                  SELECT d.id FROM recent_ids ids JOIN corpus.documents d ON d.id=ids.id
                  WHERE (%(source_types)s::text[] IS NULL OR d.source_type=ANY(%(source_types)s))
                    AND (%(force)s OR NOT EXISTS (
                      SELECT 1 FROM analytics.ai_hardware_discovery_candidates c
                      WHERE c.document_id=d.id AND c.discovered_by='rules:v2'))
                )
                SELECT
                  d.id::text AS document_id,
                  d.source_type,
                  d.source_name,
                  d.title,
                  COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) AS doc_date,
                  r.stock_code,
                  r.stock_name,
                  left(concat_ws(E'\n', d.title, d.content_text, d.content_md, d.raw_text), 22000) AS body
                FROM eligible ids JOIN corpus.documents d ON d.id=ids.id
                LEFT JOIN research.reports_resolved r ON r.document_id = d.id
                WHERE (%(source_types)s::text[] IS NULL OR d.source_type = ANY(%(source_types)s))
                  AND (%(since)s::date IS NULL OR COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) >= %(since)s::date)
                  AND (%(until)s::date IS NULL OR COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) <= %(until)s::date)
                  AND length(concat_ws(E'\n', d.title, d.content_text, d.content_md, d.raw_text)) > 40
                ORDER BY COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) DESC NULLS LAST, d.id
                LIMIT %(limit)s
                """,
                {
                    "source_types": source_types,
                    "since": args.since,
                    "until": args.until,
                    "force": args.force,
                    "limit": args.limit,
                },
            )
            docs = cur.fetchall()
            inserted = 0
            for doc in docs:
                body = clean_text(doc["body"])
                context_terms = [x for x in AI_CONTEXT_TERMS if x.lower() in body.lower()]
                if not context_terms and not any(aliases_match(body, t[0]) for t in TERM_HINTS):
                    continue
                stock_code = doc.get("stock_code")
                stock_name = doc.get("stock_name")
                for term, parent, concept_type in TERM_HINTS:
                    if not aliases_match(body, term):
                        continue
                    evidence = evidence_clause(body, term)
                    relation = infer_company_relation(evidence)
                    stock_codes = set(STOCK_CODE_RE.findall(evidence))
                    if report_stock_supported(doc, evidence, relation):
                        stock_codes.add(stock_code)
                    rule_score = score_candidate(body, evidence)
                    cur.execute(
                        """
                        INSERT INTO analytics.ai_hardware_discovery_candidates (
                          candidate_type, document_id, candidate_date, name, normalized_name,
                          parent_name, concept_type, relation_type, evidence, ai_context_terms,
                          rule_score, confidence, status, discovered_by, raw_payload, metadata, updated_at
                        )
                        VALUES ('concept', %s, %s, %s, %s, %s, %s, 'contains', %s, %s, %s, %s, 'new', 'rules:v2', %s, %s, now())
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            doc["document_id"],
                            doc["doc_date"],
                            term,
                            term,
                            parent,
                            concept_type,
                            evidence,
                            context_terms,
                            rule_score,
                            rule_score,
                            jsonb({"source_type": doc["source_type"], "source_name": doc["source_name"]}),
                            jsonb({"term_hint_parent": parent}),
                        ),
                    )
                    inserted += cur.rowcount
                    for code in stock_codes:
                        if not code:
                            continue
                        candidate_stock_name = stock_name if code == stock_code else None
                        scoped = scope_evidence(evidence,code,candidate_stock_name,[term],stock_code)
                        if not scoped['supported']:
                            continue
                        cur.execute(
                            """
                            INSERT INTO analytics.ai_hardware_discovery_candidates (
                              candidate_type, document_id, candidate_date, name, normalized_name,
                              parent_name, concept_type, stock_code, stock_name,
                              company_relation_type, evidence, ai_context_terms, rule_score,
                              confidence, status, discovered_by, raw_payload, metadata, updated_at
                            )
                            VALUES ('company_exposure', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', 'rules:v2', %s, %s, now())
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                doc["document_id"],
                                doc["doc_date"],
                                term,
                                term,
                                parent,
                                concept_type,
                                code,
                                candidate_stock_name,
                                relation,
                                scoped['span'],
                                context_terms,
                                rule_score,
                                rule_score,
                                jsonb({"source_type": doc["source_type"], "source_name": doc["source_name"]}),
                                jsonb({"term_hint_parent": parent}),
                            ),
                        )
                        inserted += cur.rowcount
            conn.commit()
    print(f"scanned={len(docs)} candidates_inserted={inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
