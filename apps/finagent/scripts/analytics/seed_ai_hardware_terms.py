# -*- coding: utf-8 -*-
"""Seed AI hardware fine-grained term dictionary."""
from __future__ import annotations

from ai_hardware_lexicon import TERMS
from common import connect, jsonb


def main() -> int:
    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            for item in TERMS:
                cur.execute(
                    """
                    INSERT INTO analytics.ai_hardware_terms (
                      term, normalized_term, subindustry, category, aliases,
                      ai_hardware_relevance, relevance_reason, is_seed, metadata, updated_at
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,true,%s,now())
                    ON CONFLICT (term) DO UPDATE SET
                      normalized_term = EXCLUDED.normalized_term,
                      subindustry = EXCLUDED.subindustry,
                      category = EXCLUDED.category,
                      aliases = EXCLUDED.aliases,
                      ai_hardware_relevance = EXCLUDED.ai_hardware_relevance,
                      relevance_reason = EXCLUDED.relevance_reason,
                      is_seed = true,
                      metadata = EXCLUDED.metadata,
                      updated_at = now()
                    """,
                    (
                        item["term"],
                        item["normalized_term"],
                        item["subindustry"],
                        item["category"],
                        item["aliases"],
                        item["ai_hardware_relevance"],
                        item["relevance_reason"],
                        jsonb({"source": "seed_ai_hardware_terms.py"}),
                    ),
                )
    print(f"seeded {len(TERMS)} AI hardware terms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
