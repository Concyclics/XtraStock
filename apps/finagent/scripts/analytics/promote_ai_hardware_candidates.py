# -*- coding: utf-8 -*-
"""Promote classified AI hardware candidates into the knowledge graph."""
from __future__ import annotations

import argparse

from common import connect, jsonb
from company_evidence_scope import scope_evidence


FINANCIAL_SERVICE_RE = (
    r"(保荐|承销|投行|IPO|并购|定增|再融资|私募基金|基金平台|资管|理财|贷款|授信|"
    r"融资支持|证券承销|银行授信|银行贷款|金融机构|券商业务)"
)


def get_or_create_concept(cur, name: str, concept_type: str, relevance: float, source: str) -> str:
    cur.execute(
        """
        INSERT INTO kg.concepts (
          concept_type, name, normalized_name, aliases, ai_hardware_relevance,
          status, source, metadata, updated_at
        )
        VALUES (%s,%s,%s,%s,%s,'active',%s,%s,now())
        ON CONFLICT (concept_type, normalized_name) DO UPDATE SET
          ai_hardware_relevance = GREATEST(kg.concepts.ai_hardware_relevance, EXCLUDED.ai_hardware_relevance),
          status = 'active',
          updated_at = now()
        RETURNING concept_id::text
        """,
        (concept_type, name, name, [name], relevance, source, jsonb({"promoted_by": "promote_ai_hardware_candidates.py"})),
    )
    return cur.fetchone()["concept_id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--include-weak", action="store_true")
    args = parser.parse_args()

    promoted = skipped = 0
    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM analytics.ai_hardware_discovery_candidates
                WHERE status = 'classified'
                  AND COALESCE(confidence, rule_score, 0) >= %s
                ORDER BY candidate_date DESC NULLS LAST, confidence DESC NULLS LAST
                LIMIT %s
                """,
                (args.min_confidence, args.limit),
            )
            rows = cur.fetchall()

            for row in rows:
                candidate_status = "promoted"
                candidate_metadata_patch = {}
                name = row.get("normalized_name") or row["name"]
                parent = row.get("parent_name") or "AI硬件"
                concept_type = row.get("concept_type") or "subsegment"
                relevance = float(row.get("confidence") or row.get("rule_score") or 0.5)
                source = row.get("discovered_by") or "candidate"
                concept_id = get_or_create_concept(cur, name, concept_type, relevance, source)
                parent_id = get_or_create_concept(cur, parent, "segment" if parent != "AI硬件" else "root", relevance, source)
                relation_type = row.get("relation_type") or "contains"
                if relation_type not in (
                    "contains", "upstream_of", "downstream_of", "used_in", "equipment_for",
                    "material_for", "component_of", "benefits_from", "risk_to", "related_to",
                ):
                    relation_type = "related_to"
                cur.execute(
                    """
                    INSERT INTO kg.concept_edges (
                      source_concept_id, target_concept_id, relation_type, confidence,
                      evidence_document_id, evidence, source, metadata, updated_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (source_concept_id, target_concept_id, relation_type) DO UPDATE SET
                      confidence = GREATEST(COALESCE(kg.concept_edges.confidence, 0), EXCLUDED.confidence),
                      evidence_document_id = COALESCE(kg.concept_edges.evidence_document_id, EXCLUDED.evidence_document_id),
                      evidence = COALESCE(kg.concept_edges.evidence, EXCLUDED.evidence),
                      updated_at = now()
                    """,
                    (parent_id, concept_id, relation_type, relevance, row.get("document_id"), row.get("evidence"), source, jsonb({"candidate_id": str(row["candidate_id"])})),
                )

                if row["candidate_type"] == "company_exposure" and row.get("stock_code"):
                    company_relation = row.get("company_relation_type") or "weak_mention"
                    evidence = row.get("evidence") or ""
                    cur.execute("SELECT r.stock_code FROM research.reports_resolved r WHERE r.document_id=%s LIMIT 1", (row.get('document_id'),))
                    source_report = cur.fetchone()
                    cur.execute("SELECT name FROM market.assets WHERE asset_type='stock' AND symbol=%s LIMIT 1", (row['stock_code'],))
                    issuer = cur.fetchone()
                    attribution = scope_evidence(evidence,row['stock_code'],row.get('stock_name') or (issuer['name'] if issuer else None),
                        [row.get('name'),row.get('normalized_name')],source_report['stock_code'] if source_report else None)
                    if not attribution['supported']:
                        candidate_metadata_patch['attribution_check'] = attribution
                        cur.execute("UPDATE analytics.ai_hardware_discovery_candidates SET status='needs_review',metadata=metadata || %s,updated_at=now() WHERE candidate_id=%s",
                                    (jsonb(candidate_metadata_patch),row['candidate_id']))
                        skipped += 1
                        continue
                    evidence = attribution['span']
                    candidate_metadata_patch['attribution_check'] = attribution
                    cur.execute("SELECT %s ~ %s AS matched", (evidence, FINANCIAL_SERVICE_RE))
                    financial_service_evidence = bool(cur.fetchone()["matched"])
                    if financial_service_evidence:
                        skipped += 1
                        candidate_status = "rejected"
                        candidate_metadata_patch["promotion_skip_reason"] = "financial_service_context"
                    elif company_relation == "weak_mention" and not args.include_weak:
                        skipped += 1
                    else:
                        cur.execute(
                            """
                            SELECT asset_id, name
                            FROM market.assets
                            WHERE asset_type = 'stock' AND symbol = %s
                            ORDER BY updated_at DESC
                            LIMIT 1
                            """,
                            (row["stock_code"],),
                        )
                        asset = cur.fetchone()
                        cur.execute(
                            """
                            INSERT INTO kg.company_concept_exposures (
                              asset_id, stock_code, stock_name, concept_id, relation_type,
                              evidence_document_id, evidence_date, evidence, confidence,
                              source, status, metadata, business_role, directness,
                              evidence_span, extractor_version, review_status, updated_at
                            )
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,'direct',%s,%s,'classified',now())
                            ON CONFLICT (
                              stock_code, concept_id, relation_type,
                              COALESCE(evidence_document_id, '00000000-0000-0000-0000-000000000000'::uuid)
                            ) DO UPDATE SET
                              stock_name = COALESCE(EXCLUDED.stock_name, kg.company_concept_exposures.stock_name),
                              confidence = GREATEST(COALESCE(kg.company_concept_exposures.confidence, 0), EXCLUDED.confidence),
                              evidence = EXCLUDED.evidence,
                              metadata = kg.company_concept_exposures.metadata || EXCLUDED.metadata,
                              business_role = EXCLUDED.business_role,
                              directness = EXCLUDED.directness,
                              evidence_span = EXCLUDED.evidence_span,
                              extractor_version = EXCLUDED.extractor_version,
                              review_status = EXCLUDED.review_status,
                              status = 'active',
                              updated_at = now()
                            """,
                            (
                                asset["asset_id"] if asset else None,
                                row["stock_code"],
                                row.get("stock_name") or (asset["name"] if asset else None),
                                concept_id,
                                company_relation,
                                row.get("document_id"),
                                row.get("candidate_date"),
                                evidence,
                                relevance,
                                source,
                                jsonb({"candidate_id": str(row["candidate_id"]),"attribution_check":attribution,"attribution_original_evidence":row.get('evidence')}),
                                company_relation,
                                evidence,
                                source,
                            ),
                        )
                cur.execute(
                    """
                    UPDATE analytics.ai_hardware_discovery_candidates
                    SET status = %s,
                        metadata = metadata || %s,
                        updated_at = now()
                    WHERE candidate_id = %s
                    """,
                    (candidate_status, jsonb(candidate_metadata_patch), row["candidate_id"]),
                )
                if candidate_status == "promoted":
                    promoted += 1
            conn.commit()
    print(f"promoted={promoted} skipped_weak={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
