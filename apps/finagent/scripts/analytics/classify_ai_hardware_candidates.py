# -*- coding: utf-8 -*-
"""Classify AI hardware discovery candidates with a pluggable LLM."""
from __future__ import annotations

import argparse
import json
import re
from typing import Any

from common import connect, jsonb
from llm_client import LLMError, get_llm_client


PROMPT_VERSION = "ai_hardware_candidate_classification_v2"

FINANCIAL_SERVICE_RE = re.compile(
    r"(保荐|承销|投行|IPO|并购|定增|再融资|私募基金|基金平台|资管|理财|贷款|授信|"
    r"融资支持|证券承销|银行授信|银行贷款|金融机构|券商业务)"
)

SYSTEM_PROMPT = """你是A股AI硬件产业链知识图谱分类器。只输出JSON。
任务：判断候选概念或候选公司关系是否与AI硬件产业链相关，并给出父节点、关系类型和置信度。
允许的concept_type: segment, subsegment, material, equipment, component, system, process, application, risk。
允许的concept relation_type: contains, upstream_of, used_in, equipment_for, material_for, component_of, benefits_from, risk_to, related_to。
允许的company_relation_type: direct_product, material_supplier, equipment_supplier, downstream_customer, weak_mention。
原则：公司只有在证据句明确说明其产品/材料/设备/业务与候选概念相关时，才给direct_product/material_supplier/equipment_supplier；仅同篇文章列名给weak_mention。
泛词如电源、氦气、二氧化碳、材料，必须有半导体、AI服务器、数据中心、先进封装等上下文才算相关。
金融服务语境如保荐、承销、IPO、贷款、授信、基金投资，只能算融资/服务关系，不能把金融机构判成AI硬件公司、材料供应商或设备供应商。"""


def clamp(value: Any, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def classify_by_rules(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") or ""
    relation = row.get("company_relation_type") or "weak_mention"
    financial_service_context = row["candidate_type"] == "company_exposure" and bool(FINANCIAL_SERVICE_RE.search(evidence))
    if financial_service_context:
        relation = "weak_mention"
    if row["candidate_type"] == "company_exposure" and relation == "weak_mention":
        score = min(float(row.get("rule_score") or 0.4), 0.55)
    else:
        score = max(float(row.get("rule_score") or 0.55), 0.65)
    if any(x in evidence for x in ("AI服务器", "算力", "GPU", "HBM", "数据中心", "先进封装", "光模块", "CPO")):
        score = min(1.0, score + 0.15)
    return {
        "is_ai_hardware_related": score >= 0.45 and not financial_service_context,
        "normalized_name": row.get("normalized_name") or row.get("name"),
        "parent_name": row.get("parent_name") or "AI硬件",
        "concept_type": row.get("concept_type") or "subsegment",
        "relation_type": row.get("relation_type") or "contains",
        "company_relation_type": relation,
        "relevance_score": score,
        "confidence": score,
        "reason": "financial service context is not a supply-chain exposure" if financial_service_context else "rules-only classification",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--provider", help="Override LLM_PROVIDER, e.g. mock or openai_compatible.")
    parser.add_argument("--rules-only", action="store_true")
    parser.add_argument("--model")
    args = parser.parse_args()

    client = None
    if not args.rules_only:
        client = get_llm_client(args.provider)

    ok = failed = 0
    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            model_name = args.model or args.provider or ("rules-only" if args.rules_only else "llm")
            cur.execute(
                """
                INSERT INTO ai.extraction_runs (model_name, prompt_version, task_type, status, config)
                VALUES (%s, %s, 'ai_hardware_candidate_classification', 'running', %s)
                RETURNING id
                """,
                (model_name, PROMPT_VERSION, jsonb({"limit": args.limit, "rules_only": args.rules_only})),
            )
            run_id = cur.fetchone()["id"]
            conn.commit()

            cur.execute(
                """
                SELECT *
                FROM analytics.ai_hardware_discovery_candidates
                WHERE status IN ('new', 'needs_review')
                ORDER BY candidate_date DESC NULLS LAST, rule_score DESC NULLS LAST, created_at
                LIMIT %s
                """,
                (args.limit,),
            )
            rows = cur.fetchall()

            for row in rows:
                try:
                    if args.rules_only:
                        parsed = classify_by_rules(row)
                        raw_response = parsed
                    else:
                        messages = [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "candidate_type": row["candidate_type"],
                                        "name": row["name"],
                                        "parent_name": row.get("parent_name"),
                                        "stock_code": row.get("stock_code"),
                                        "stock_name": row.get("stock_name"),
                                        "evidence": row.get("evidence"),
                                        "ai_context_terms": row.get("ai_context_terms") or [],
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ]
                        result = client.chat_json(messages, PROMPT_VERSION, model=args.model)
                        parsed = result["parsed"]
                        raw_response = result["raw_response"]
                    if row["candidate_type"] == "company_exposure" and FINANCIAL_SERVICE_RE.search(row.get("evidence") or ""):
                        parsed["is_ai_hardware_related"] = False
                        parsed["company_relation_type"] = "weak_mention"
                        parsed["reason"] = "financial service context rejected by deterministic policy"
                    related = bool(parsed.get("is_ai_hardware_related", True))
                    confidence = clamp(parsed.get("confidence", parsed.get("relevance_score")), row.get("rule_score") or 0.5)
                    status = "classified" if related and confidence >= 0.45 else "rejected"
                    cur.execute(
                        """
                        UPDATE analytics.ai_hardware_discovery_candidates
                        SET normalized_name = COALESCE(%s, normalized_name),
                            parent_name = COALESCE(%s, parent_name),
                            concept_type = COALESCE(%s, concept_type),
                            relation_type = COALESCE(%s, relation_type),
                            company_relation_type = COALESCE(%s, company_relation_type),
                            llm_score = %s,
                            confidence = %s,
                            status = %s,
                            llm_run_id = %s,
                            raw_payload = raw_payload || %s,
                            metadata = metadata || %s,
                            updated_at = now()
                        WHERE candidate_id = %s
                        """,
                        (
                            parsed.get("normalized_name"),
                            parsed.get("parent_name"),
                            parsed.get("concept_type"),
                            parsed.get("relation_type"),
                            parsed.get("company_relation_type"),
                            clamp(parsed.get("relevance_score"), confidence),
                            confidence,
                            status,
                            run_id,
                            jsonb({"classification": raw_response}),
                            jsonb({"classification_reason": parsed.get("reason")}),
                            row["candidate_id"],
                        ),
                    )
                    ok += 1
                    conn.commit()
                except (LLMError, Exception) as exc:
                    failed += 1
                    conn.rollback()
                    with conn.cursor() as fail_cur:
                        fail_cur.execute(
                            """
                            UPDATE analytics.ai_hardware_discovery_candidates
                            SET status = 'needs_review',
                                metadata = metadata || %s,
                                updated_at = now()
                            WHERE candidate_id = %s
                            """,
                            (jsonb({"classification_error": str(exc)[:500]}), row["candidate_id"]),
                        )
                        fail_cur.execute("UPDATE ai.extraction_runs SET status = 'partial' WHERE id = %s", (run_id,))
                        conn.commit()

            with conn.cursor() as cur2:
                cur2.execute(
                    "UPDATE ai.extraction_runs SET status = %s, finished_at = now() WHERE id = %s",
                    ("ok" if failed == 0 else "partial", run_id),
                )
                conn.commit()
    print(f"run_id={run_id} classified={ok} failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
