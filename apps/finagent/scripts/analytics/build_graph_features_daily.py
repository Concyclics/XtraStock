#!/usr/bin/env python3
"""Build point-in-time company features from KG relation claims."""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect


SQL = """
WITH params AS (
  SELECT %(as_of)s::date AS as_of,
         (%(as_of)s::date + time '23:59:59') AT TIME ZONE 'Asia/Shanghai' AS cutoff
), companies AS (
  SELECT e.entity_id, e.source_ref::uuid AS asset_id
  FROM kg.market_entities e
  JOIN market.assets a ON a.asset_id=e.source_ref::uuid
  WHERE e.entity_type='company' AND e.source_ref ~* '^[0-9a-f-]{36}$'
    AND a.asset_type='stock'
), visible_claims AS (
  SELECT c.*
  FROM kg.relation_claims c CROSS JOIN params p
  WHERE c.known_at <= p.cutoff
    AND (c.expired_at IS NULL OR c.expired_at > p.cutoff)
    AND c.valid_from <= p.as_of
    AND (c.valid_to IS NULL OR c.valid_to >= p.as_of)
    AND c.status IN ('candidate','active')
), exposure AS (
  SELECT co.asset_id,
         count(*) FILTER (WHERE c.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO','PRICE_LINKED_TO'))::int AS exposure_count,
         coalesce(sum(c.confidence * coalesce(c.weight, 1.0) *
           CASE coalesce(c.directness,'unknown') WHEN 'direct' THEN 1.0 WHEN 'indirect' THEN 0.5 ELSE 0.7 END)
           FILTER (WHERE c.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO','PRICE_LINKED_TO')), 0) AS direct_exposure_score
  FROM companies co
  LEFT JOIN visible_claims c ON c.source_entity_id=co.entity_id
  GROUP BY co.asset_id
), recent_claims AS (
  SELECT co.asset_id, c.*,
         coalesce((c.metadata->>'polarity')::int, 0) AS polarity
  FROM companies co
  JOIN kg.relation_claims c
    ON c.source_entity_id=co.entity_id OR c.target_entity_id=co.entity_id
  CROSS JOIN params p
  WHERE c.known_at <= p.cutoff
    AND (c.expired_at IS NULL OR c.expired_at > p.cutoff)
    AND c.valid_from BETWEEN p.as_of - 29 AND p.as_of
    AND c.status IN ('candidate','active')
), recent AS (
  SELECT asset_id,
         coalesce(sum(rc.confidence * coalesce(rc.weight,1.0))
           FILTER (WHERE rc.relation_type='AFFECTED_BY_EVENT' AND rc.polarity > 0), 0) AS positive_event_score_30d,
         coalesce(sum(abs(rc.confidence * coalesce(rc.weight,1.0)))
           FILTER (WHERE rc.relation_type='AFFECTED_BY_EVENT' AND rc.polarity < 0), 0) AS negative_event_score_30d,
         count(DISTINCT rc.source_entity_id) FILTER (WHERE rc.relation_type='COVERS_COMPANY')::int AS institution_coverage_30d,
         count(DISTINCT ce.document_id) FILTER (WHERE ce.document_id IS NOT NULL)::int AS evidence_document_count_30d,
         count(DISTINCT rc.claim_id)::int AS new_relation_count_30d
  FROM recent_claims rc
  LEFT JOIN kg.claim_evidence ce ON ce.claim_id=rc.claim_id
  GROUP BY asset_id
), final AS (
  SELECT co.asset_id, p.as_of AS dt,
         coalesce(e.direct_exposure_score,0) AS direct_exposure_score,
         coalesce(e.exposure_count,0) AS exposure_count,
         coalesce(r.positive_event_score_30d,0) AS positive_event_score_30d,
         coalesce(r.negative_event_score_30d,0) AS negative_event_score_30d,
         coalesce(r.institution_coverage_30d,0) AS institution_coverage_30d,
         coalesce(r.evidence_document_count_30d,0) AS evidence_document_count_30d,
         coalesce(r.new_relation_count_30d,0) AS new_relation_count_30d,
         ln(1 + coalesce(r.new_relation_count_30d,0)) *
           ln(1 + coalesce(r.evidence_document_count_30d,0)) AS novelty_score_30d,
         ln(1 + coalesce(e.direct_exposure_score,0))
           + coalesce(r.positive_event_score_30d,0)
           - coalesce(r.negative_event_score_30d,0)
           + 0.2 * ln(1 + coalesce(r.institution_coverage_30d,0))
           + 0.1 * ln(1 + coalesce(r.new_relation_count_30d,0)) AS graph_score,
         p.cutoff AS available_at,
         jsonb_build_object('claim_cutoff', p.cutoff, 'lookback_days', 30) AS provenance
  FROM companies co CROSS JOIN params p
  LEFT JOIN exposure e ON e.asset_id=co.asset_id
  LEFT JOIN recent r ON r.asset_id=co.asset_id
)
INSERT INTO analytics.graph_feature_daily (
  asset_id, dt, direct_exposure_score, exposure_count,
  positive_event_score_30d, negative_event_score_30d,
  institution_coverage_30d, evidence_document_count_30d,
  new_relation_count_30d, novelty_score_30d, graph_score,
  available_at, provenance, updated_at
)
SELECT asset_id, dt, direct_exposure_score, exposure_count,
       positive_event_score_30d, negative_event_score_30d,
       institution_coverage_30d, evidence_document_count_30d,
       new_relation_count_30d, novelty_score_30d, graph_score,
       available_at, provenance, now()
FROM final
ON CONFLICT (asset_id, dt) DO UPDATE SET
  direct_exposure_score=EXCLUDED.direct_exposure_score,
  exposure_count=EXCLUDED.exposure_count,
  positive_event_score_30d=EXCLUDED.positive_event_score_30d,
  negative_event_score_30d=EXCLUDED.negative_event_score_30d,
  institution_coverage_30d=EXCLUDED.institution_coverage_30d,
  evidence_document_count_30d=EXCLUDED.evidence_document_count_30d,
  new_relation_count_30d=EXCLUDED.new_relation_count_30d,
  novelty_score_30d=EXCLUDED.novelty_score_30d,
  graph_score=EXCLUDED.graph_score,
  available_at=EXCLUDED.available_at,
  provenance=EXCLUDED.provenance,
  updated_at=now()
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    args = parser.parse_args()
    as_of = dt.date.fromisoformat(args.as_of)
    with connect(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL, {"as_of": as_of})
            print(f"graph_feature_daily: upserted={cur.rowcount} as_of={as_of}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
