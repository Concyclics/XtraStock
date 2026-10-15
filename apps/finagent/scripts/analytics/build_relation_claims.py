#!/usr/bin/env python3
"""Materialize auditable bitemporal claims from the compatible market KG."""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--lookback-days", type=int, default=90)
    args = parser.parse_args()
    as_of = dt.date.fromisoformat(args.as_of)
    since = as_of - dt.timedelta(days=max(1, args.lookback_days))

    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kg.relation_claims (
                  claim_key, source_entity_id, target_entity_id, relation_type,
                  valid_from, valid_to, known_at, expired_at, direction,
                  confidence, weight, lag_days, horizon_days, directness,
                  source_system, extractor_version, review_status, status, metadata, updated_at
                )
                SELECT 'relation:' || r.relation_key,
                       r.source_entity_id, r.target_entity_id, r.relation_type,
                       r.valid_from, r.valid_to,
                       coalesce(d.published_at, d.created_at, r.observed_at),
                       CASE WHEN r.status='expired' THEN r.updated_at END,
                       CASE WHEN coalesce(r.metadata->>'direction','') ~ '^-?[01]$'
                            THEN (r.metadata->>'direction')::smallint
                            ELSE rt.default_direction END,
                       coalesce(r.confidence, 0.5), r.weight,
                       coalesce((r.metadata->>'lag_days')::int, rt.default_lag_days),
                       coalesce((r.metadata->>'horizon_days')::int, rt.default_horizon_days),
                       r.metadata->>'directness', r.source_system,
                       coalesce(r.extractor_version, 'unknown'),
                       CASE r.metadata->>'review_status'
                         WHEN 'approved' THEN 'approved' WHEN 'rejected' THEN 'rejected'
                         WHEN 'disputed' THEN 'disputed' ELSE 'unreviewed' END,
                       CASE r.status WHEN 'active' THEN 'active' WHEN 'expired' THEN 'expired'
                         WHEN 'rejected' THEN 'rejected' ELSE 'candidate' END,
                       r.metadata || jsonb_build_object('legacy_relation_id', r.relation_id), now()
                FROM kg.market_entity_relations r
                JOIN kg.relation_types rt ON rt.relation_type=r.relation_type
                LEFT JOIN corpus.documents d ON d.id=r.evidence_document_id
                WHERE r.valid_from <= %(as_of)s
                  AND (r.valid_to IS NULL OR r.valid_to >= %(since)s)
                ON CONFLICT (claim_key) DO UPDATE SET
                  valid_to=EXCLUDED.valid_to, expired_at=EXCLUDED.expired_at,
                  direction=EXCLUDED.direction, confidence=EXCLUDED.confidence,
                  weight=EXCLUDED.weight, lag_days=EXCLUDED.lag_days,
                  horizon_days=EXCLUDED.horizon_days, directness=EXCLUDED.directness,
                  review_status=EXCLUDED.review_status, status=EXCLUDED.status,
                  metadata=kg.relation_claims.metadata || EXCLUDED.metadata,
                  updated_at=now()
                """,
                {"since": since, "as_of": as_of},
            )
            claim_count = cur.rowcount
            cur.execute(
                """
                INSERT INTO kg.claim_evidence (
                  claim_id, document_id, source_published_at, evidence_text,
                  confidence, evidence_hash, metadata
                )
                SELECT c.claim_id, r.evidence_document_id,
                       coalesce(d.published_at, d.created_at), r.evidence,
                       r.confidence,
                       md5(coalesce(r.evidence_document_id::text,'') || ':' || coalesce(r.evidence,'')),
                       jsonb_build_object('legacy_relation_id', r.relation_id)
                FROM kg.market_entity_relations r
                JOIN kg.relation_claims c ON c.claim_key='relation:' || r.relation_key
                LEFT JOIN corpus.documents d ON d.id=r.evidence_document_id
                WHERE r.evidence_document_id IS NOT NULL OR nullif(r.evidence,'') IS NOT NULL
                ON CONFLICT (claim_id, evidence_hash) DO UPDATE SET
                  confidence=EXCLUDED.confidence,
                  evidence_text=EXCLUDED.evidence_text,
                  source_published_at=EXCLUDED.source_published_at
                """
            )
            evidence_count = cur.rowcount
        conn.commit()
    print(f"as_of={as_of} claims={claim_count} evidence={evidence_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
