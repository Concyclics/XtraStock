#!/usr/bin/env python3
"""Build evidence-backed temporal knowledge-graph relations for the full market."""

from __future__ import annotations

import argparse
import datetime as dt

from common import connect


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--lookback-days", type=int, default=30)
    args = parser.parse_args()
    as_of = dt.date.fromisoformat(args.as_of)
    since = as_of - dt.timedelta(days=max(1, min(args.lookback_days, 3650)))

    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kg.market_entities (entity_type, canonical_key, name, normalized_name, aliases, source_ref, metadata, updated_at)
                SELECT 'company', 'asset:' || a.asset_id::text, coalesce(a.name, a.symbol),
                       lower(coalesce(a.name, a.symbol)), ARRAY[a.symbol]::text[], a.asset_id::text,
                       jsonb_build_object('stock_code', a.symbol, 'exchange', a.exchange, 'asset_type', a.asset_type), now()
                FROM market.assets a WHERE a.asset_type='stock'
                ON CONFLICT (canonical_key) DO UPDATE SET
                  name=EXCLUDED.name, normalized_name=EXCLUDED.normalized_name,
                  aliases=EXCLUDED.aliases, source_ref=EXCLUDED.source_ref,
                  metadata=kg.market_entities.metadata || EXCLUDED.metadata, status='active', updated_at=now()
                WHERE (kg.market_entities.name,kg.market_entities.normalized_name,kg.market_entities.aliases,kg.market_entities.source_ref,kg.market_entities.metadata,kg.market_entities.status) IS DISTINCT FROM (EXCLUDED.name,EXCLUDED.normalized_name,EXCLUDED.aliases,EXCLUDED.source_ref,kg.market_entities.metadata || EXCLUDED.metadata,'active')
                """
            )
            cur.execute(
                """
                INSERT INTO kg.market_entities (entity_type, canonical_key, name, normalized_name, source_ref, metadata, updated_at)
                SELECT DISTINCT 'industry', 'industry:' || md5(lower(trim(industry_name))),
                       trim(industry_name), lower(trim(industry_name)), trim(industry_name),
                       jsonb_build_object('source', industry_source), now()
                FROM analytics.asset_industry_current
                WHERE asset_type='stock' AND nullif(trim(industry_name), '') IS NOT NULL
                ON CONFLICT (canonical_key) DO UPDATE SET
                  name=EXCLUDED.name, source_ref=EXCLUDED.source_ref,
                  metadata=kg.market_entities.metadata || EXCLUDED.metadata, status='active', updated_at=now()
                WHERE (kg.market_entities.name,kg.market_entities.source_ref,kg.market_entities.metadata,kg.market_entities.status) IS DISTINCT FROM (EXCLUDED.name,EXCLUDED.source_ref,kg.market_entities.metadata || EXCLUDED.metadata,'active')
                """
            )
            cur.execute(
                """
                INSERT INTO kg.market_entities (entity_type, canonical_key, name, normalized_name, aliases, source_ref, metadata, updated_at)
                SELECT CASE c.concept_type
                         WHEN 'component' THEN 'product'
                         WHEN 'material' THEN 'material'
                         WHEN 'equipment' THEN 'equipment'
                         ELSE 'concept'
                       END,
                       'concept:' || c.concept_id::text, c.name, c.normalized_name,
                       c.aliases, c.concept_id::text,
                       jsonb_build_object('concept_type', c.concept_type, 'legacy_source', c.source), now()
                FROM kg.concepts c WHERE c.status='active'
                ON CONFLICT (canonical_key) DO UPDATE SET
                  entity_type=EXCLUDED.entity_type,
                  name=EXCLUDED.name, normalized_name=EXCLUDED.normalized_name,
                  aliases=EXCLUDED.aliases, metadata=kg.market_entities.metadata || EXCLUDED.metadata,
                  status='active', updated_at=now()
                WHERE (kg.market_entities.entity_type,kg.market_entities.name,kg.market_entities.normalized_name,kg.market_entities.aliases,kg.market_entities.metadata,kg.market_entities.status) IS DISTINCT FROM (EXCLUDED.entity_type,EXCLUDED.name,EXCLUDED.normalized_name,EXCLUDED.aliases,kg.market_entities.metadata || EXCLUDED.metadata,'active')
                """
            )
            cur.execute(
                """
                INSERT INTO kg.market_entities (entity_type, canonical_key, name, normalized_name, source_ref, updated_at)
                SELECT DISTINCT 'institution', 'institution:' || md5(lower(trim(r.institution))),
                       trim(r.institution), lower(trim(r.institution)), trim(r.institution), now()
                FROM research.reports_resolved r
                WHERE r.report_date BETWEEN %(since)s AND %(as_of)s
                  AND nullif(trim(r.institution), '') IS NOT NULL
                ON CONFLICT (canonical_key) DO UPDATE SET name=EXCLUDED.name, status='active', updated_at=now()
                WHERE (kg.market_entities.name,kg.market_entities.status) IS DISTINCT FROM (EXCLUDED.name,'active')
                """,
                {"since": since, "as_of": as_of},
            )
            cur.execute(
                """
                INSERT INTO kg.market_entities (entity_type, canonical_key, name, normalized_name, source_ref, metadata, updated_at)
                SELECT 'event', 'event:' || e.id::text,
                       left(coalesce(nullif(e.summary, ''), e.event_type), 300), lower(e.event_type), e.id::text,
                       jsonb_build_object('event_type', e.event_type, 'polarity', e.polarity, 'impact_score', e.impact_score), now()
                FROM analytics.document_events e
                WHERE e.event_date BETWEEN %(since)s AND %(as_of)s
                ON CONFLICT (canonical_key) DO UPDATE SET
                  name=EXCLUDED.name, metadata=kg.market_entities.metadata || EXCLUDED.metadata, updated_at=now()
                WHERE (kg.market_entities.name,kg.market_entities.metadata) IS DISTINCT FROM (EXCLUDED.name,kg.market_entities.metadata || EXCLUDED.metadata)
                """,
                {"since": since, "as_of": as_of},
            )

            # Close only superseded current industry memberships.
            cur.execute(
                """
                UPDATE kg.market_entity_relations old
                SET valid_to=%(as_of)s::date - 1, status='expired', updated_at=now()
                FROM kg.market_entities company
                WHERE old.source_entity_id=company.entity_id
                  AND old.relation_type='IN_INDUSTRY' AND old.source_system='asset_industry_current'
                  AND old.status='active' AND old.valid_to IS NULL
                  AND NOT EXISTS (
                    SELECT 1
                    FROM analytics.asset_industry_current ai
                    JOIN kg.market_entities industry
                      ON industry.canonical_key='industry:' || md5(lower(trim(ai.industry_name)))
                    WHERE ai.asset_id::text=company.source_ref
                      AND nullif(trim(ai.industry_name), '') IS NOT NULL
                      AND industry.entity_id=old.target_entity_id
                  )
                """,
                {"as_of": as_of},
            )
            cur.execute(
                """
                DELETE FROM kg.market_entity_relations
                WHERE source_system='document_events' AND extractor_version='rules:v1'
                """
            )
            cur.execute(
                """
                INSERT INTO kg.market_entity_relations (
                  relation_key, source_entity_id, target_entity_id, relation_type,
                  valid_from, evidence_date, confidence, evidence, source_system, extractor_version, metadata
                )
                SELECT 'industry:' || ai.asset_id::text || ':' || md5(lower(trim(ai.industry_name))) || ':' ||
                         coalesce(ai.industry_source_date, %(as_of)s)::text,
                       company.entity_id, industry.entity_id, 'IN_INDUSTRY',
                       coalesce(ai.industry_source_date, %(as_of)s), coalesce(ai.industry_source_date, %(as_of)s),
                       CASE ai.industry_source WHEN 'market.assets' THEN 0.95 ELSE 0.7 END,
                       ai.industry_source, 'asset_industry_current', 'market_kg:v1',
                       jsonb_build_object('stock_code', ai.stock_code, 'industry_source', ai.industry_source)
                FROM analytics.asset_industry_current ai
                JOIN kg.market_entities company ON company.canonical_key='asset:' || ai.asset_id::text
                JOIN kg.market_entities industry ON industry.canonical_key='industry:' || md5(lower(trim(ai.industry_name)))
                WHERE ai.asset_type='stock' AND nullif(trim(ai.industry_name), '') IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM kg.market_entity_relations current
                    WHERE current.source_entity_id=company.entity_id
                      AND current.target_entity_id=industry.entity_id
                      AND current.relation_type='IN_INDUSTRY'
                      AND current.status='active' AND current.valid_to IS NULL
                  )
                ON CONFLICT (relation_key) DO UPDATE SET observed_at=now(), updated_at=now(), status='active'
                """,
                {"as_of": as_of},
            )
            cur.execute(
                """
                INSERT INTO kg.market_entity_relations (
                  relation_key, source_entity_id, target_entity_id, relation_type,
                  valid_from, evidence_date, confidence, evidence_document_id,
                  evidence, source_system, extractor_version, metadata
                )
                SELECT 'concept-edge:' || e.edge_id::text,
                       source_entity.entity_id, target_entity.entity_id,
                       upper(e.relation_type),
                       coalesce(d.published_at::date, e.created_at::date),
                       coalesce(d.published_at::date, e.created_at::date),
                       coalesce(e.confidence, 0.7), e.evidence_document_id,
                       e.evidence, 'concept_edges', 'market_kg:v3',
                       e.metadata || jsonb_build_object('legacy_edge_id', e.edge_id)
                FROM kg.concept_edges e
                JOIN kg.market_entities source_entity
                  ON source_entity.canonical_key='concept:' || e.source_concept_id::text
                JOIN kg.market_entities target_entity
                  ON target_entity.canonical_key='concept:' || e.target_concept_id::text
                LEFT JOIN corpus.documents d ON d.id=e.evidence_document_id
                WHERE e.relation_type='contains'
                ON CONFLICT (relation_key) DO UPDATE SET
                  confidence=EXCLUDED.confidence, evidence=EXCLUDED.evidence,
                  metadata=EXCLUDED.metadata, observed_at=now(), updated_at=now(), status='active'
                """
            )
            cur.execute(
                """
                INSERT INTO kg.market_entity_relations (
                  relation_key, source_entity_id, target_entity_id, relation_type,
                  valid_from, evidence_date, confidence, evidence_document_id, evidence,
                  source_system, extractor_version, metadata
                )
                SELECT 'exposure:' || x.exposure_id::text, company.entity_id, concept.entity_id,
                       'EXPOSED_TO', coalesce(x.evidence_date, %(as_of)s), coalesce(x.evidence_date, %(as_of)s),
                       x.confidence, x.evidence_document_id, coalesce(x.evidence_span, x.evidence),
                       'company_concept_exposures', coalesce(x.extractor_version, 'legacy'),
                       jsonb_build_object('relation_type', x.relation_type, 'directness', x.directness,
                                          'business_role', x.business_role, 'review_status', x.review_status)
                FROM kg.company_concept_exposures x
                JOIN market.assets a ON a.symbol=x.stock_code AND a.asset_type='stock'
                JOIN kg.market_entities company ON company.canonical_key='asset:' || a.asset_id::text
                JOIN kg.market_entities concept ON concept.canonical_key='concept:' || x.concept_id::text
                WHERE x.status='active' AND coalesce(x.evidence_date, %(as_of)s) <= %(as_of)s
                ON CONFLICT (relation_key) DO UPDATE SET
                  confidence=EXCLUDED.confidence, evidence=EXCLUDED.evidence,
                  metadata=EXCLUDED.metadata, observed_at=now(), updated_at=now()
                """,
                {"as_of": as_of},
            )
            cur.execute(
                """
                INSERT INTO kg.market_entity_relations (
                  relation_key, source_entity_id, target_entity_id, relation_type,
                  valid_from, valid_to, evidence_date, confidence, weight,
                  evidence_document_id, evidence, source_system, extractor_version, metadata
                )
                SELECT 'event-link:' || e.id::text || ':' || a.asset_id::text,
                       company.entity_id, event.entity_id, 'AFFECTED_BY_EVENT',
                       e.event_date, e.event_date, e.event_date, e.confidence, e.impact_score,
                       e.document_id, coalesce(e.evidence, e.summary), 'document_events', e.extraction_method,
                       jsonb_build_object('event_type', e.event_type, 'polarity', e.polarity)
                FROM analytics.document_events e
                CROSS JOIN LATERAL unnest(e.stock_codes) code(stock_code)
                JOIN market.assets a ON a.symbol=code.stock_code AND a.asset_type='stock'
                JOIN kg.market_entities company ON company.canonical_key='asset:' || a.asset_id::text
                JOIN kg.market_entities event ON event.canonical_key='event:' || e.id::text
                WHERE e.event_date BETWEEN %(since)s AND %(as_of)s
                  AND e.extraction_method <> 'rules:v1'
                ON CONFLICT (relation_key) DO UPDATE SET
                  confidence=EXCLUDED.confidence, weight=EXCLUDED.weight,
                  evidence=EXCLUDED.evidence, metadata=EXCLUDED.metadata, observed_at=now(), updated_at=now()
                """,
                {"since": since, "as_of": as_of},
            )
            cur.execute(
                """
                INSERT INTO kg.market_entity_relations (
                  relation_key, source_entity_id, target_entity_id, relation_type,
                  valid_from, valid_to, evidence_date, confidence, evidence_document_id,
                  evidence, source_system, extractor_version, metadata
                )
                SELECT 'coverage:' || r.document_id::text || ':' || a.asset_id::text || ':' || md5(lower(trim(r.institution))),
                       institution.entity_id, company.entity_id, 'COVERS_COMPANY',
                       r.report_date, r.report_date, r.report_date, 1.0, r.document_id,
                       r.report_title, 'research.reports', 'market_kg:v1',
                       jsonb_build_object('rating', r.rating, 'rating_change', r.rating_change, 'report_type', r.report_type)
                FROM research.reports_resolved r
                JOIN market.assets a ON a.symbol=r.stock_code AND a.asset_type='stock'
                JOIN kg.market_entities company ON company.canonical_key='asset:' || a.asset_id::text
                JOIN kg.market_entities institution ON institution.canonical_key='institution:' || md5(lower(trim(r.institution)))
                WHERE r.report_date BETWEEN %(since)s AND %(as_of)s
                  AND nullif(trim(r.institution), '') IS NOT NULL
                ON CONFLICT (relation_key) DO UPDATE SET
                  evidence=EXCLUDED.evidence, metadata=EXCLUDED.metadata, observed_at=now(), updated_at=now()
                """,
                {"since": since, "as_of": as_of},
            )

            cur.execute("DELETE FROM analytics.kg_relation_daily_stats WHERE as_of_date=%s", (as_of,))
            cur.execute(
                """
                INSERT INTO analytics.kg_relation_daily_stats (
                  as_of_date, relation_type, source_entity_type, target_entity_type,
                  relation_count, distinct_source_count, distinct_target_count, avg_confidence, updated_at
                )
                SELECT %(as_of)s, r.relation_type, s.entity_type, t.entity_type,
                       count(*), count(DISTINCT r.source_entity_id), count(DISTINCT r.target_entity_id),
                       avg(r.confidence), now()
                FROM kg.market_entity_relations r
                JOIN kg.market_entities s ON s.entity_id=r.source_entity_id
                JOIN kg.market_entities t ON t.entity_id=r.target_entity_id
                WHERE r.status='active' AND r.valid_from <= %(as_of)s
                  AND (r.valid_to IS NULL OR r.valid_to >= %(as_of)s)
                GROUP BY r.relation_type, s.entity_type, t.entity_type
                """,
                {"as_of": as_of},
            )
            counts = cur.execute(
                """
                SELECT (SELECT count(*) FROM kg.market_entities) AS entities,
                       (SELECT count(*) FROM kg.market_entity_relations) AS relations,
                       (SELECT count(*) FROM analytics.kg_relation_daily_stats WHERE as_of_date=%s) AS stat_groups
                """,
                (as_of,),
            ).fetchone()
        conn.commit()
    print(f"as_of={as_of} entities={counts['entities']} relations={counts['relations']} stat_groups={counts['stat_groups']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
