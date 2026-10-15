# -*- coding: utf-8 -*-
"""Seed the AI hardware concept graph."""
from __future__ import annotations

from ai_hardware_graph_seed import CONCEPTS, EDGES
from common import connect, jsonb


def main() -> int:
    concept_ids: dict[str, str] = {}
    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            for typ, name, normalized, aliases, relevance, description in CONCEPTS:
                cur.execute(
                    """
                    INSERT INTO kg.concepts (
                      concept_type, name, normalized_name, aliases, description,
                      ai_hardware_relevance, status, source, metadata, updated_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,'active','seed:v2',%s,now())
                    ON CONFLICT (concept_type, normalized_name) DO UPDATE SET
                      name = EXCLUDED.name,
                      aliases = EXCLUDED.aliases,
                      description = EXCLUDED.description,
                      ai_hardware_relevance = EXCLUDED.ai_hardware_relevance,
                      status = 'active',
                      source = 'seed:v2',
                      metadata = kg.concepts.metadata || EXCLUDED.metadata,
                      updated_at = now()
                    RETURNING concept_id::text
                    """,
                    (typ, name, normalized, aliases, description, relevance, jsonb({"seed": "ai_hardware_graph_v2"})),
                )
                concept_ids[normalized] = cur.fetchone()["concept_id"]

            for source_name, target_name, relation_type in EDGES:
                source_id = concept_ids[source_name]
                target_id = concept_ids[target_name]
                cur.execute(
                    """
                    INSERT INTO kg.concept_edges (
                      source_concept_id, target_concept_id, relation_type,
                      confidence, source, evidence, metadata, updated_at
                    )
                    VALUES (%s,%s,%s,1.0,'seed:v2','seed hierarchy',%s,now())
                    ON CONFLICT (source_concept_id, target_concept_id, relation_type) DO UPDATE SET
                      confidence = EXCLUDED.confidence,
                      source = EXCLUDED.source,
                      evidence = EXCLUDED.evidence,
                      metadata = kg.concept_edges.metadata || EXCLUDED.metadata,
                      updated_at = now()
                    """,
                    (source_id, target_id, relation_type, jsonb({"seed": "ai_hardware_graph_v2"})),
                )
    print(f"seeded concepts={len(CONCEPTS)} edges={len(EDGES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
