#!/usr/bin/env python3
"""Build current bitemporal heads protected by immutable revision history.

The source tables remain the extraction system of record. This job creates
deterministic relation/version keys so any date range can be replayed safely.
"""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect
from graph_revision_policy import GRAPH_WRITE_LOCK, RECONCILE_EXPOSURES_SQL, acquire_graph_write_lock
from graph_source_review import review_current


ENTITY_SQL = """
INSERT INTO kg.market_entities
  (entity_type, canonical_key, name, normalized_name, aliases, source_ref, metadata, updated_at)
SELECT 'company', 'asset:' || a.asset_id::text, coalesce(a.name,a.symbol),
       lower(coalesce(a.name,a.symbol)), ARRAY[a.symbol]::text[] || ARRAY(SELECT x.old_code FROM market.research_security_code_aliases x WHERE x.asset_id=a.asset_id ORDER BY x.old_code), a.asset_id::text,
       jsonb_build_object('stock_code',a.symbol,'exchange',a.exchange,'asset_type',a.asset_type), now()
FROM market.assets a WHERE a.asset_type='stock'
ON CONFLICT (canonical_key) DO UPDATE SET
  name=EXCLUDED.name, normalized_name=EXCLUDED.normalized_name,
  aliases=EXCLUDED.aliases, source_ref=EXCLUDED.source_ref,
  metadata=kg.market_entities.metadata || EXCLUDED.metadata, status='active', updated_at=now();

INSERT INTO kg.market_entities
  (entity_type, canonical_key, name, normalized_name, source_ref, metadata, updated_at)
SELECT DISTINCT ON (lower(trim(r.institution))) 'institution', 'institution:' || md5(lower(trim(r.institution))),
       trim(r.institution), lower(trim(r.institution)), trim(r.institution),
       jsonb_build_object('source','research.reports'), now()
FROM research.reports_resolved r
WHERE r.report_date BETWEEN %(since)s AND %(until)s
  AND nullif(trim(r.institution),'') IS NOT NULL
ON CONFLICT (canonical_key) DO UPDATE SET name=EXCLUDED.name,
  normalized_name=EXCLUDED.normalized_name, status='active', updated_at=now();

INSERT INTO kg.market_entities
  (entity_type, canonical_key, name, normalized_name, source_ref, metadata, updated_at)
SELECT 'industry', 'industry:' || md5(lower(trim(x.industry_name))),
       min(trim(x.industry_name)), lower(trim(x.industry_name)), min(trim(x.industry_name)),
       jsonb_build_object('source',min(x.source_name)), now()
FROM (
  SELECT industry_name, 'research.reports'::text AS source_name
  FROM research.reports_resolved WHERE report_date BETWEEN %(since)s AND %(until)s
  UNION ALL
  SELECT unnest(industries), 'analytics.document_events'
  FROM analytics.document_events WHERE event_date BETWEEN %(since)s AND %(until)s
) x
WHERE nullif(trim(x.industry_name),'') IS NOT NULL
GROUP BY lower(trim(x.industry_name))
ON CONFLICT (canonical_key) DO UPDATE SET name=EXCLUDED.name,
  normalized_name=EXCLUDED.normalized_name,
  metadata=kg.market_entities.metadata || EXCLUDED.metadata, status='active', updated_at=now();

INSERT INTO kg.market_entities
  (entity_type, canonical_key, name, normalized_name, aliases, source_ref, metadata, updated_at)
SELECT CASE c.concept_type WHEN 'component' THEN 'product' WHEN 'material' THEN 'material'
         WHEN 'equipment' THEN 'equipment' WHEN 'process' THEN 'technology' ELSE 'concept' END,
       'concept:' || c.concept_id::text, c.name, c.normalized_name, c.aliases, c.concept_id::text,
       jsonb_build_object('concept_type',c.concept_type,'ai_hardware_relevance',c.ai_hardware_relevance,
                          'legacy_source',c.source), now()
FROM kg.concepts c WHERE c.status='active'
ON CONFLICT (canonical_key) DO UPDATE SET entity_type=EXCLUDED.entity_type,
  name=EXCLUDED.name, normalized_name=EXCLUDED.normalized_name, aliases=EXCLUDED.aliases,
  metadata=kg.market_entities.metadata || EXCLUDED.metadata, status='active', updated_at=now();
"""


RESEARCH_SQL = """
WITH base AS (
  SELECT r.*, d.published_at, d.created_at AS document_created_at,
         coalesce(r.available_at,d.published_at,
           (r.report_date::timestamp + time '23:59:59') AT TIME ZONE 'Asia/Shanghai') AS public_at,
         (SELECT min(r2.report_date) FROM research.reports_resolved r2
          WHERE r2.stock_code=r.stock_code AND lower(trim(r2.institution))=lower(trim(r.institution))
            AND r2.report_date>r.report_date) AS next_date
  FROM research.reports_resolved r JOIN corpus.documents d ON d.id=r.document_id
  WHERE r.report_type='stock' AND r.report_date BETWEEN %(since)s AND %(until)s
    AND nullif(trim(r.institution),'') IS NOT NULL AND nullif(r.stock_code,'') IS NOT NULL
), pairs AS (
  SELECT b.*, i.entity_id AS institution_id, c.entity_id AS company_id
  FROM base b
  JOIN market.assets a ON a.asset_type='stock' AND a.symbol=b.stock_code
  JOIN kg.market_entities c ON c.canonical_key='asset:' || a.asset_id::text
  JOIN kg.market_entities i ON i.canonical_key='institution:' || md5(lower(trim(b.institution)))
), relation_input AS (
  SELECT DISTINCT 'covers:' || institution_id::text || ':' || company_id::text AS relation_key,
         institution_id AS source_id, company_id AS target_id, 'COVERS_COMPANY'::text AS relation_type FROM pairs
  UNION
  SELECT DISTINCT 'rates:' || institution_id::text || ':' || company_id::text,
         institution_id, company_id, 'RATES' FROM pairs
  UNION
  SELECT DISTINCT 'forecasts:' || institution_id::text || ':' || company_id::text,
         institution_id, company_id, 'FORECASTS' FROM pairs
)
INSERT INTO kg.relations (relation_key,source_entity_id,target_entity_id,relation_type,updated_at)
SELECT relation_key,source_id,target_id,relation_type,now() FROM relation_input
ON CONFLICT (relation_key) DO UPDATE SET updated_at=now();

WITH base AS (
  SELECT r.*, d.published_at, d.created_at AS document_created_at,
         coalesce(r.available_at,d.published_at,
           (r.report_date::timestamp + time '23:59:59') AT TIME ZONE 'Asia/Shanghai') AS public_at,
         (SELECT min(r2.report_date) FROM research.reports_resolved r2
          WHERE r2.stock_code=r.stock_code AND lower(trim(r2.institution))=lower(trim(r.institution))
            AND r2.report_date>r.report_date) AS next_date
  FROM research.reports_resolved r JOIN corpus.documents d ON d.id=r.document_id
  WHERE r.report_type='stock' AND r.report_date BETWEEN %(since)s AND %(until)s
    AND nullif(trim(r.institution),'') IS NOT NULL AND nullif(r.stock_code,'') IS NOT NULL
), pairs AS (
  SELECT b.*, i.entity_id AS institution_id, c.entity_id AS company_id
  FROM base b JOIN market.assets a ON a.asset_type='stock' AND a.symbol=b.stock_code
  JOIN kg.market_entities c ON c.canonical_key='asset:' || a.asset_id::text
  JOIN kg.market_entities i ON i.canonical_key='institution:' || md5(lower(trim(b.institution)))
), rating AS (
  SELECT DISTINCT ON (p.document_id) p.*,
         coalesce(rr.rating,p.rating) AS fact_rating,
         coalesce(rr.rating_change,p.rating_change) AS fact_rating_change,
         rr.previous_rating, rr.target_price, rr.confidence AS rating_confidence,
         rr.created_at AS rating_created_at
  FROM pairs p LEFT JOIN research.report_ratings rr ON rr.document_id=p.document_id
  ORDER BY p.document_id, (rr.target_price IS NOT NULL) DESC, rr.confidence DESC NULLS LAST, rr.id
), forecast AS (
  SELECT p.document_id,
         jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
           'forecast_year',f.forecast_year,'revenue',f.revenue,'net_profit',f.net_profit,
           'eps',f.eps,'pe',f.pe,'currency',f.currency,'source',f.source,'confidence',f.confidence,
           'semantic_contract',f.metadata->>'semantic_contract','year_verified',f.metadata->'year_verified',
           'eps_unit','CNY/share','net_profit_unit',f.metadata->>'net_profit_unit','net_profit_scope',f.metadata->>'net_profit_scope'
         )) ORDER BY f.forecast_year,f.source) AS forecasts,
         max(greatest(f.created_at,f.updated_at)) AS forecast_created_at,
         avg(f.confidence) FILTER (WHERE f.confidence IS NOT NULL) AS forecast_confidence
  FROM pairs p JOIN research.report_forecasts_usable f ON f.document_id=p.document_id GROUP BY p.document_id
), versions AS (
  SELECT rel.relation_id, 'research:coverage:' || p.document_id::text AS version_key,
         daterange(p.report_date,p.report_date+1,'[)') AS valid_during,
         tstzrange(p.public_at,NULL,'[)') AS public_known_during,
         tstzrange(greatest(p.public_at,p.created_at,p.document_created_at,p.identity_known_at),NULL,'[)') AS system_known_during,
         0::smallint AS direction, 1.0::double precision AS confidence, 1.0::double precision AS weight,
         'COVERS_COMPANY'::text AS relation_type, p.document_id,
         jsonb_strip_nulls(jsonb_build_object('document_id',p.document_id,'report_date',p.report_date,
           'report_title',p.report_title,'stock_code',p.stock_code,'institution',p.institution)) AS attributes
  FROM pairs p JOIN kg.relations rel ON rel.relation_key='covers:' || p.institution_id::text || ':' || p.company_id::text
  UNION ALL
  SELECT rel.relation_id, 'research:rating:' || r.document_id::text,
         daterange(r.report_date,least(coalesce(r.next_date,r.report_date+181),r.report_date+181),'[)'),
         tstzrange(r.public_at,NULL,'[)'),
         tstzrange(greatest(r.public_at,r.created_at,r.document_created_at,r.identity_known_at,coalesce(r.rating_created_at,r.created_at)),NULL,'[)'),
         CASE WHEN r.fact_rating_change ~ '(下调|降低)' THEN -1 WHEN r.fact_rating_change ~ '(上调|调高)' THEN 1 ELSE 0 END::smallint,
         coalesce(r.rating_confidence::double precision,0.85), 1.0::double precision, 'RATES', r.document_id,
         jsonb_strip_nulls(jsonb_build_object('document_id',r.document_id,'report_date',r.report_date,
           'rating',r.fact_rating,'rating_change',r.fact_rating_change,'previous_rating',r.previous_rating,
           'target_price',r.target_price,'stock_code',r.stock_code,'institution',r.institution))
  FROM rating r JOIN kg.relations rel ON rel.relation_key='rates:' || r.institution_id::text || ':' || r.company_id::text
  WHERE nullif(r.fact_rating,'') IS NOT NULL OR nullif(r.fact_rating_change,'') IS NOT NULL OR r.target_price IS NOT NULL
  UNION ALL
  SELECT rel.relation_id, 'research:forecast:' || p.document_id::text,
         daterange(p.report_date,least(coalesce(p.next_date,p.report_date+181),p.report_date+181),'[)'),
         tstzrange(p.public_at,NULL,'[)'),
         tstzrange(greatest(p.public_at,p.created_at,p.document_created_at,p.identity_known_at,f.forecast_created_at),NULL,'[)'),
         0::smallint, coalesce(f.forecast_confidence::double precision,0.8), 1.0::double precision,
         'FORECASTS', p.document_id,
         jsonb_build_object('document_id',p.document_id,'report_date',p.report_date,'stock_code',p.stock_code,
           'institution',p.institution,'forecasts',f.forecasts,'forecast_semantics','earnings-forecast-v2')
  FROM pairs p JOIN forecast f ON f.document_id=p.document_id
  JOIN kg.relations rel ON rel.relation_key='forecasts:' || p.institution_id::text || ':' || p.company_id::text
)
INSERT INTO kg.relation_versions (
  relation_id,version_key,valid_during,public_known_during,system_known_during,direction,
  confidence,weight,status,review_status,source_system,extractor_version,attributes
)
SELECT relation_id,version_key,valid_during,public_known_during,system_known_during,direction,
       greatest(0,least(confidence,1)),weight,'active','unreviewed','research.reports','temporal_research:v1',attributes
FROM versions WHERE NOT isempty(valid_during)
ON CONFLICT (relation_id,version_key) DO UPDATE SET
  valid_during=EXCLUDED.valid_during, public_known_during=EXCLUDED.public_known_during,
  system_known_during=EXCLUDED.system_known_during, direction=EXCLUDED.direction,
  confidence=EXCLUDED.confidence, attributes=jsonb_strip_nulls(jsonb_build_object('evidence_review',kg.relation_versions.attributes->'evidence_review'))||EXCLUDED.attributes, status='active';

WITH evidence_input AS (
  SELECT v.relation_version_id,(v.attributes->>'document_id')::uuid AS document_id,
         d.published_at AS public_at, greatest(d.created_at,v.created_at) AS system_at,
         left(coalesce(d.content_text,d.content_md,d.raw_text,d.title),1200) AS evidence_text,
         md5(v.relation_version_id::text || ':' || d.id::text) AS evidence_hash,
         v.confidence, jsonb_build_object('source_name',d.source_name,'source_type',d.source_type) AS attributes
  FROM kg.relation_versions v JOIN corpus.documents d ON d.id=(v.attributes->>'document_id')::uuid
  WHERE v.extractor_version='temporal_research:v1'
    AND (d.published_at AT TIME ZONE 'Asia/Shanghai')::date BETWEEN %(since)s AND %(until)s
)
INSERT INTO kg.relation_version_evidence
  (relation_version_id,document_id,public_at,system_at,evidence_text,confidence,evidence_hash,attributes)
SELECT relation_version_id,document_id,public_at,system_at,evidence_text,confidence,evidence_hash,attributes FROM evidence_input
ON CONFLICT (relation_version_id,evidence_hash) DO UPDATE SET
  evidence_text=EXCLUDED.evidence_text,confidence=EXCLUDED.confidence,attributes=EXCLUDED.attributes;

WITH classifications AS (
  SELECT r.document_id,r.report_date,r.stock_code,trim(r.industry_name) AS industry_name,
         coalesce(r.available_at,d.published_at,(r.report_date::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai') AS public_at,
         greatest(r.created_at,d.created_at,r.identity_known_at) AS system_base,
         c.entity_id AS company_id,i.entity_id AS industry_id
  FROM research.reports_resolved r JOIN corpus.documents d ON d.id=r.document_id
  JOIN market.assets a ON a.asset_type='stock' AND a.symbol=r.stock_code
  JOIN kg.market_entities c ON c.canonical_key='asset:' || a.asset_id::text
  JOIN kg.market_entities i ON i.canonical_key='industry:' || md5(lower(trim(r.industry_name)))
  WHERE r.report_date BETWEEN %(since)s AND %(until)s AND r.report_type='stock'
    AND nullif(trim(r.industry_name),'') IS NOT NULL
), ins_rel AS (
  INSERT INTO kg.relations (relation_key,source_entity_id,target_entity_id,relation_type,updated_at)
  SELECT DISTINCT 'classified:' || company_id::text || ':' || industry_id::text,
         company_id,industry_id,'CLASSIFIED_AS',now() FROM classifications
  ON CONFLICT (relation_key) DO UPDATE SET updated_at=now() RETURNING relation_id,relation_key
)
INSERT INTO kg.relation_versions
  (relation_id,version_key,valid_during,public_known_during,system_known_during,direction,confidence,weight,
   status,review_status,source_system,extractor_version,attributes)
SELECT rel.relation_id,'research:classification:' || c.document_id::text,
       daterange(c.report_date,c.report_date+1,'[)'),tstzrange(c.public_at,NULL,'[)'),
       tstzrange(greatest(c.public_at,c.system_base),NULL,'[)'),0,0.75,1,'active','unreviewed',
       'research.reports','temporal_research:v1',
       jsonb_build_object('document_id',c.document_id,'report_date',c.report_date,
                          'stock_code',c.stock_code,'industry_name',c.industry_name)
FROM classifications c JOIN ins_rel rel
  ON rel.relation_key='classified:' || c.company_id::text || ':' || c.industry_id::text
ON CONFLICT (relation_id,version_key) DO UPDATE SET attributes=jsonb_strip_nulls(jsonb_build_object('evidence_review',kg.relation_versions.attributes->'evidence_review'))||EXCLUDED.attributes,status='active';
"""


EVENT_RECONCILE_SQL = """
WITH current_pairs AS MATERIALIZED (
  SELECT company.entity_id AS source_id, event.entity_id AS target_id
  FROM analytics.document_events e
  JOIN kg.market_entities event ON event.canonical_key='event:' || e.id::text
  CROSS JOIN LATERAL (
    SELECT unnest(e.asset_ids) AS asset_id
    UNION SELECT a.asset_id FROM unnest(e.stock_codes) s(code)
      JOIN market.assets a ON a.asset_type='stock' AND a.symbol=s.code
  ) x
  JOIN market.assets a ON a.asset_id=x.asset_id AND a.asset_type='stock'
  JOIN kg.market_entities company ON company.canonical_key='asset:' || a.asset_id::text
  WHERE e.event_date BETWEEN %(since)s AND %(until)s
  UNION
  SELECT industry.entity_id,event.entity_id
  FROM analytics.document_events e
  JOIN kg.market_entities event ON event.canonical_key='event:' || e.id::text
  CROSS JOIN LATERAL unnest(e.industries) industry_name
  JOIN kg.market_entities industry ON industry.canonical_key='industry:' || md5(lower(trim(industry_name)))
  WHERE e.event_date BETWEEN %(since)s AND %(until)s AND nullif(trim(industry_name),'') IS NOT NULL
)
UPDATE kg.relation_versions v SET status='archived'
FROM kg.relations r
WHERE r.relation_id=v.relation_id AND v.extractor_version='temporal_events:v1'
  AND lower(v.valid_during) BETWEEN %(since)s AND %(until)s
  AND v.status<>'archived'
  AND NOT EXISTS(SELECT 1 FROM current_pairs p
    WHERE p.source_id=r.source_entity_id AND p.target_id=r.target_entity_id);
"""


EVENT_SQL = """
INSERT INTO kg.market_entities
  (entity_type,canonical_key,name,normalized_name,source_ref,metadata,updated_at)
SELECT 'event','event:' || e.id::text,left(coalesce(nullif(e.summary,''),e.event_type),300),
       lower(e.event_type),e.id::text,
       jsonb_build_object('event_type',e.event_type,'polarity',e.polarity,'impact_score',e.impact_score),now()
FROM analytics.document_events e WHERE e.event_date BETWEEN %(since)s AND %(until)s
ON CONFLICT (canonical_key) DO UPDATE SET name=EXCLUDED.name,
  metadata=kg.market_entities.metadata || EXCLUDED.metadata,status='active',updated_at=now()
WHERE (kg.market_entities.name,kg.market_entities.metadata,kg.market_entities.status)
 IS DISTINCT FROM (EXCLUDED.name,kg.market_entities.metadata || EXCLUDED.metadata,'active');
""" + EVENT_RECONCILE_SQL + """

WITH event_assets AS (
  SELECT DISTINCT e.id,e.document_id,e.event_date,e.event_type,e.polarity,e.impact_score,e.confidence,
         e.summary,e.evidence,e.extraction_method,e.created_at,e.updated_at,a.asset_id
  FROM analytics.document_events e
  CROSS JOIN LATERAL (
    SELECT unnest(e.asset_ids) AS asset_id
    UNION SELECT a2.asset_id FROM unnest(e.stock_codes) s(code)
      JOIN market.assets a2 ON a2.asset_type='stock' AND a2.symbol=s.code
  ) x JOIN market.assets a ON a.asset_id=x.asset_id AND a.asset_type='stock'
  WHERE e.event_date BETWEEN %(since)s AND %(until)s
), pairs AS (
  SELECT ea.*,c.entity_id AS company_id,ev.entity_id AS event_id,
         coalesce(d.published_at,(ea.event_date::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai') AS public_at,
         coalesce(d.created_at,ea.created_at) AS document_created_at
  FROM event_assets ea
  JOIN kg.market_entities c ON c.canonical_key='asset:' || ea.asset_id::text
  JOIN kg.market_entities ev ON ev.canonical_key='event:' || ea.id::text
  LEFT JOIN corpus.documents d ON d.id=ea.document_id
), ins_rel AS (
  INSERT INTO kg.relations (relation_key,source_entity_id,target_entity_id,relation_type,updated_at)
  SELECT 'event-impact:' || company_id::text || ':' || event_id::text,
         company_id,event_id,'AFFECTED_BY_EVENT',now() FROM pairs
  ON CONFLICT (relation_key) DO UPDATE SET updated_at=now() RETURNING relation_id,relation_key
)
INSERT INTO kg.relation_versions
  (relation_id,version_key,valid_during,public_known_during,system_known_during,direction,confidence,weight,
   horizon_days,status,review_status,source_system,extractor_version,attributes)
SELECT rel.relation_id,'document-event:' || p.id::text,
       daterange(p.event_date,p.event_date+1,'[)'),tstzrange(p.public_at,NULL,'[)'),
       tstzrange(greatest(p.public_at,p.document_created_at,p.created_at),NULL,'[)'),p.polarity::smallint,
       greatest(0,least(coalesce(p.confidence,0.5),1)),coalesce(p.impact_score,1),30,
       'active','unreviewed','analytics.document_events','temporal_events:v1',
       jsonb_strip_nulls(jsonb_build_object('document_id',p.document_id,'event_id',p.id,'event_date',p.event_date,
         'event_type',p.event_type,'polarity',p.polarity,'summary',p.summary,'extraction_method',p.extraction_method))
FROM pairs p JOIN ins_rel rel
  ON rel.relation_key='event-impact:' || p.company_id::text || ':' || p.event_id::text
ON CONFLICT (relation_id,version_key) DO UPDATE SET valid_during=EXCLUDED.valid_during,
  public_known_during=EXCLUDED.public_known_during,direction=EXCLUDED.direction,confidence=EXCLUDED.confidence,
  weight=EXCLUDED.weight,attributes=jsonb_strip_nulls(jsonb_build_object('evidence_review',kg.relation_versions.attributes->'evidence_review'))||EXCLUDED.attributes,status='active';

WITH pairs AS (
  SELECT e.*,trim(industry_name) AS industry_name,i.entity_id AS industry_id,ev.entity_id AS event_id,
         coalesce(d.published_at,(e.event_date::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai') AS public_at,
         coalesce(d.created_at,e.created_at) AS document_created_at
  FROM analytics.document_events e CROSS JOIN LATERAL unnest(e.industries) industry_name
  JOIN kg.market_entities i ON i.canonical_key='industry:' || md5(lower(trim(industry_name)))
  JOIN kg.market_entities ev ON ev.canonical_key='event:' || e.id::text
  LEFT JOIN corpus.documents d ON d.id=e.document_id
  WHERE e.event_date BETWEEN %(since)s AND %(until)s AND nullif(trim(industry_name),'') IS NOT NULL
), ins_rel AS (
  INSERT INTO kg.relations (relation_key,source_entity_id,target_entity_id,relation_type,updated_at)
  SELECT 'industry-event:' || industry_id::text || ':' || event_id::text,
         industry_id,event_id,'AFFECTED_BY_EVENT',now() FROM pairs
  ON CONFLICT (relation_key) DO UPDATE SET updated_at=now() RETURNING relation_id,relation_key
)
INSERT INTO kg.relation_versions
  (relation_id,version_key,valid_during,public_known_during,system_known_during,direction,confidence,weight,
   horizon_days,status,review_status,source_system,extractor_version,attributes)
SELECT rel.relation_id,'document-event-industry:' || p.id::text || ':' || md5(lower(p.industry_name)),
       daterange(p.event_date,p.event_date+1,'[)'),tstzrange(p.public_at,NULL,'[)'),
       tstzrange(greatest(p.public_at,p.document_created_at,p.created_at),NULL,'[)'),p.polarity::smallint,
       greatest(0,least(coalesce(p.confidence,0.5),1)),coalesce(p.impact_score,1),30,
       'active','unreviewed','analytics.document_events','temporal_events:v1',
       jsonb_strip_nulls(jsonb_build_object('document_id',p.document_id,'event_id',p.id,'event_date',p.event_date,
         'event_type',p.event_type,'polarity',p.polarity,'industry_name',p.industry_name,
         'summary',p.summary,'extraction_method',p.extraction_method))
FROM pairs p JOIN ins_rel rel
  ON rel.relation_key='industry-event:' || p.industry_id::text || ':' || p.event_id::text
ON CONFLICT (relation_id,version_key) DO UPDATE SET valid_during=EXCLUDED.valid_during,
  public_known_during=EXCLUDED.public_known_during,direction=EXCLUDED.direction,confidence=EXCLUDED.confidence,
  weight=EXCLUDED.weight,attributes=jsonb_strip_nulls(jsonb_build_object('evidence_review',kg.relation_versions.attributes->'evidence_review'))||EXCLUDED.attributes,status='active';

INSERT INTO kg.relation_version_evidence
  (relation_version_id,document_id,public_at,system_at,evidence_text,confidence,evidence_hash,attributes)
SELECT v.relation_version_id,e.document_id,d.published_at,greatest(e.created_at,coalesce(d.created_at,e.created_at)),
       left(e.evidence,1200),e.confidence,
       md5(v.relation_version_id::text || ':' || coalesce(e.document_id::text,e.id::text)),
       jsonb_build_object('event_id',e.id,'extraction_method',e.extraction_method)
FROM kg.relation_versions v
JOIN analytics.document_events e ON e.id=(v.attributes->>'event_id')::uuid
LEFT JOIN corpus.documents d ON d.id=e.document_id
WHERE v.extractor_version='temporal_events:v1' AND e.event_date BETWEEN %(since)s AND %(until)s
ON CONFLICT (relation_version_id,evidence_hash) DO UPDATE SET evidence_text=EXCLUDED.evidence_text;
"""


SUPPLY_SQL = """

WITH pairs AS (
  SELECT x.*,ce.entity_id AS company_id,ke.entity_id AS concept_entity_id,
         coalesce(d.published_at,(coalesce(x.evidence_date,x.created_at::date)::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai') AS public_at,
         coalesce(d.created_at,x.created_at) AS document_created_at
  FROM kg.company_concept_exposures x
  JOIN market.assets a ON a.asset_type='stock' AND a.symbol=x.stock_code
  JOIN kg.market_entities ce ON ce.canonical_key='asset:' || a.asset_id::text
  JOIN kg.market_entities ke ON ke.canonical_key='concept:' || x.concept_id::text
  LEFT JOIN corpus.documents d ON d.id=x.evidence_document_id
  WHERE x.status IN ('candidate','active')
    AND coalesce(x.evidence_date,x.created_at::date) BETWEEN %(since)s AND %(until)s
), ins_rel AS (
  INSERT INTO kg.relations (relation_key,source_entity_id,target_entity_id,relation_type,updated_at)
  SELECT DISTINCT 'exposure:' || company_id::text || ':' || concept_entity_id::text,
         company_id,concept_entity_id,'EXPOSED_TO',now() FROM pairs
  ON CONFLICT (relation_key) DO UPDATE SET updated_at=now() RETURNING relation_id,relation_key
)
INSERT INTO kg.relation_versions
  (relation_id,version_key,valid_during,public_known_during,system_known_during,direction,confidence,weight,directness,
   horizon_days,status,review_status,source_system,extractor_version,attributes)
SELECT rel.relation_id,'concept-exposure:' || p.exposure_id::text,
       daterange(coalesce(p.evidence_date,p.created_at::date),
         CASE WHEN p.evidence_document_id IS NULL THEN NULL ELSE coalesce(p.evidence_date,p.created_at::date)+366 END,'[)'),
       tstzrange(p.public_at,NULL,'[)'),tstzrange(greatest(p.public_at,p.document_created_at,p.created_at),NULL,'[)'),
       0,greatest(0,least(coalesce(p.confidence,0.5),1)),1,p.directness,365,
       CASE WHEN p.status='active' AND p.metadata->'attribution_check'->>'supported'='true' THEN 'active' ELSE 'candidate' END,
       CASE p.review_status WHEN 'approved' THEN 'approved' WHEN 'rejected' THEN 'rejected'
         WHEN 'disputed' THEN 'disputed' ELSE 'unreviewed' END,
       'kg.company_concept_exposures',coalesce(p.extractor_version,'temporal_supply:v1'),
       jsonb_strip_nulls(jsonb_build_object('document_id',p.evidence_document_id,'exposure_id',p.exposure_id,
         'stock_code',p.stock_code,'relation_type',p.relation_type,'business_role',p.business_role,
         'attribution_check',coalesce(p.metadata->'attribution_check',jsonb_build_object('supported',false,'reason','missing_source_attribution')),
         'source_review_status',p.status,'review_contract','graph-pit-v1','superseded_by_canonical',false))
FROM pairs p JOIN ins_rel rel
  ON rel.relation_key='exposure:' || p.company_id::text || ':' || p.concept_entity_id::text
ON CONFLICT (relation_id,version_key) DO UPDATE SET confidence=EXCLUDED.confidence,
  review_status=CASE WHEN EXCLUDED.review_status='unreviewed' AND EXCLUDED.status='active' AND kg.relation_versions.review_status='classified' THEN 'classified' ELSE EXCLUDED.review_status END,status=EXCLUDED.status,attributes=jsonb_strip_nulls(jsonb_build_object('evidence_review',kg.relation_versions.attributes->'evidence_review'))||EXCLUDED.attributes;

WITH edges AS (
  SELECT e.*,s.entity_id AS source_id,t.entity_id AS target_id,
         coalesce(d.published_at,e.created_at) AS public_at,
         CASE e.relation_type WHEN 'contains' THEN 'CONTAINS' WHEN 'upstream_of' THEN 'UPSTREAM_OF'
           WHEN 'downstream_of' THEN 'DOWNSTREAM_OF' WHEN 'component_of' THEN 'COMPONENT_OF'
           WHEN 'used_in' THEN 'USED_IN' WHEN 'material_for' THEN 'USED_IN'
           WHEN 'equipment_for' THEN 'USED_IN' ELSE 'EXPOSED_TO' END AS temporal_type
  FROM kg.concept_edges e
  JOIN kg.market_entities s ON s.canonical_key='concept:' || e.source_concept_id::text
  JOIN kg.market_entities t ON t.canonical_key='concept:' || e.target_concept_id::text
  LEFT JOIN corpus.documents d ON d.id=e.evidence_document_id
  WHERE e.created_at::date <= %(until)s AND s.entity_id<>t.entity_id
), ins_rel AS (
  INSERT INTO kg.relations (relation_key,source_entity_id,target_entity_id,relation_type,updated_at)
  SELECT 'concept-edge:' || source_id::text || ':' || target_id::text || ':' || temporal_type,
         source_id,target_id,temporal_type,now() FROM edges
  ON CONFLICT (relation_key) DO UPDATE SET updated_at=now() RETURNING relation_id,relation_key
)
INSERT INTO kg.relation_versions
  (relation_id,version_key,valid_during,public_known_during,system_known_during,direction,confidence,weight,
   status,review_status,source_system,extractor_version,attributes)
SELECT rel.relation_id,'concept-edge:' || e.edge_id::text,
       daterange(coalesce((e.metadata->>'valid_from')::date,e.created_at::date),NULL,'[)'),
       tstzrange(e.public_at,NULL,'[)'),tstzrange(greatest(e.public_at,e.created_at),NULL,'[)'),
       CASE WHEN e.temporal_type IN ('UPSTREAM_OF','DOWNSTREAM_OF') THEN 1 ELSE 0 END,
       greatest(0,least(coalesce(e.confidence,0.7),1)),1,'active','unreviewed','kg.concept_edges',
       'temporal_supply:v1',jsonb_strip_nulls(jsonb_build_object('document_id',e.evidence_document_id,
         'edge_id',e.edge_id,'legacy_relation_type',e.relation_type))
FROM edges e JOIN ins_rel rel
  ON rel.relation_key='concept-edge:' || e.source_id::text || ':' || e.target_id::text || ':' || e.temporal_type
ON CONFLICT (relation_id,version_key) DO UPDATE SET confidence=EXCLUDED.confidence,attributes=jsonb_strip_nulls(jsonb_build_object('evidence_review',kg.relation_versions.attributes->'evidence_review'))||EXCLUDED.attributes;
"""


ENTITY_VERSION_SQL = """
WITH scoped_entities AS MATERIALIZED (
  SELECT e.* FROM kg.market_entities e WHERE e.entity_type<>'event'
  UNION ALL
  SELECT e.* FROM analytics.document_events ev
  JOIN kg.market_entities e ON e.canonical_key='event:' || ev.id::text
  WHERE ev.event_date BETWEEN %(since)s AND %(until)s AND e.entity_type='event'
), inferred AS (
  SELECT e.entity_id,e.name,e.aliases,e.metadata,
         daterange(coalesce(a.listed_date,'1900-01-01'::date),NULL,'[)') AS valid_during,
         coalesce((a.listed_date::timestamp AT TIME ZONE 'Asia/Shanghai'),e.created_at) AS public_at,
         e.created_at AS system_at,'market.assets'::text AS source_system,1 AS priority
  FROM scoped_entities e JOIN market.assets a ON e.canonical_key='asset:' || a.asset_id::text
  WHERE e.entity_type='company'
  UNION ALL
  SELECT e.entity_id,e.name,e.aliases,e.metadata,
         daterange(ev.event_date,ev.event_date+1,'[)'),
         coalesce(d.published_at,(ev.event_date::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai'),
         greatest(ev.created_at,coalesce(d.created_at,ev.created_at)),
         'analytics.document_events',1
  FROM scoped_entities e JOIN analytics.document_events ev ON e.canonical_key='event:' || ev.id::text
  LEFT JOIN corpus.documents d ON d.id=ev.document_id WHERE ev.event_date BETWEEN %(since)s AND %(until)s
  UNION ALL
  SELECT e.entity_id,e.name,e.aliases,e.metadata,daterange(min(r.report_date),NULL,'[)'),
         min(coalesce(r.available_at,d.published_at,(r.report_date::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai')),
         min(greatest(r.created_at,d.created_at)),'research.reports',1
  FROM scoped_entities e JOIN research.reports_resolved r
    ON e.canonical_key='institution:' || md5(lower(trim(r.institution)))
  JOIN corpus.documents d ON d.id=r.document_id WHERE r.report_date IS NOT NULL
  GROUP BY e.entity_id,e.name,e.aliases,e.metadata
  UNION ALL
  SELECT e.entity_id,e.name,e.aliases,e.metadata,daterange(min(r.report_date),NULL,'[)'),
         min(coalesce(r.available_at,d.published_at,(r.report_date::timestamp+time '23:59:59') AT TIME ZONE 'Asia/Shanghai')),
         min(greatest(r.created_at,d.created_at)),'research.reports:industry',1
  FROM scoped_entities e JOIN research.reports_resolved r
    ON e.canonical_key='industry:' || md5(lower(trim(r.industry_name)))
  JOIN corpus.documents d ON d.id=r.document_id WHERE r.report_date IS NOT NULL
  GROUP BY e.entity_id,e.name,e.aliases,e.metadata
  UNION ALL
  SELECT e.entity_id,e.name,e.aliases,e.metadata,
         daterange(min(coalesce(x.evidence_date,x.created_at::date)),NULL,'[)'),
         min(coalesce(d.published_at,x.created_at)),min(greatest(x.created_at,coalesce(d.created_at,x.created_at))),
         'kg.company_concept_exposures',1
  FROM scoped_entities e JOIN kg.company_concept_exposures x
    ON e.canonical_key='concept:' || x.concept_id::text
  LEFT JOIN corpus.documents d ON d.id=x.evidence_document_id
  GROUP BY e.entity_id,e.name,e.aliases,e.metadata
), selected AS (
  SELECT DISTINCT ON (e.entity_id) e.entity_id,e.name,e.aliases,e.metadata,
         coalesce(i.valid_during,daterange('1900-01-01'::date,NULL,'[)')) AS valid_during,
         coalesce(i.public_at,e.created_at) AS public_at,
         greatest(coalesce(i.system_at,e.created_at),coalesce(i.public_at,e.created_at)) AS system_at,
         coalesce(i.source_system,'kg.market_entities') AS source_system
  FROM scoped_entities e LEFT JOIN inferred i ON i.entity_id=e.entity_id
  ORDER BY e.entity_id,i.priority,i.public_at
)
INSERT INTO kg.entity_versions
  (entity_id,version_key,valid_during,public_known_during,system_known_during,name,aliases,attributes,source_system,extractor_version)
SELECT s.entity_id,'canonical:v1',s.valid_during,tstzrange(s.public_at,NULL,'[)'),
       tstzrange(s.system_at,NULL,'[)'),s.name,s.aliases,s.metadata,s.source_system,'temporal_entities:v2'
FROM selected s
ON CONFLICT (entity_id,version_key) DO UPDATE SET valid_during=EXCLUDED.valid_during,
  public_known_during=EXCLUDED.public_known_during,system_known_during=EXCLUDED.system_known_during,
  name=EXCLUDED.name,aliases=EXCLUDED.aliases,attributes=EXCLUDED.attributes,
  source_system=EXCLUDED.source_system,extractor_version=EXCLUDED.extractor_version;
"""


COVERAGE_SQL = """
INSERT INTO kg.source_coverage_daily
  (source_name,dt,expected_documents,rules_processed,llm_processed,relation_count,status,extractor_version,details,updated_at)
SELECT d.source_name,x.dt,count(DISTINCT d.id),
       count(DISTINCT s.document_id) FILTER (WHERE s.extraction_method LIKE 'rules:%%' AND s.status='ok'),
       count(DISTINCT s.document_id) FILTER (WHERE s.extraction_method NOT LIKE 'rules:%%' AND s.status='ok'),
       count(DISTINCT v.relation_version_id),
       CASE WHEN count(DISTINCT s.document_id) FILTER (WHERE s.extraction_method LIKE 'rules:%%' AND s.status='ok') >= count(DISTINCT d.id)
            THEN 'complete' WHEN count(DISTINCT s.document_id)>0 THEN 'partial' ELSE 'pending' END,
       'temporal_coverage:v1',jsonb_build_object('semantics','document extraction coverage'),now()
FROM (
  SELECT id,source_name,coalesce((published_at AT TIME ZONE 'Asia/Shanghai')::date,created_at::date) AS dt
  FROM corpus.documents
  WHERE coalesce((published_at AT TIME ZONE 'Asia/Shanghai')::date,created_at::date) BETWEEN %(since)s AND %(until)s
) d
JOIN LATERAL (SELECT d.dt) x ON true
LEFT JOIN analytics.document_events e ON e.document_id=d.id
LEFT JOIN kg.document_extraction_status s ON s.document_id=d.id
LEFT JOIN kg.relation_version_evidence ve ON ve.document_id=d.id
LEFT JOIN kg.relation_versions v ON v.relation_version_id=ve.relation_version_id
GROUP BY d.source_name,x.dt
ON CONFLICT (source_name,dt) DO UPDATE SET
  expected_documents=EXCLUDED.expected_documents,rules_processed=EXCLUDED.rules_processed,
  llm_processed=EXCLUDED.llm_processed,relation_count=EXCLUDED.relation_count,status=EXCLUDED.status,
  extractor_version=EXCLUDED.extractor_version,details=EXCLUDED.details,updated_at=now();
"""


def execute_batch(cur, sql: str, params: dict[str, object]) -> int:
    """Execute the fixed SQL statements separately for psycopg prepared mode."""
    total = 0
    for statement in (part.strip() for part in sql.split(";\n")):
        if not statement:
            continue
        cur.execute(statement, params)
        total += max(cur.rowcount, 0)
    return total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument(
        "--stages", default="research,events,supply,entities,coverage",
        help="Comma-separated: research,events,supply,entities,coverage.",
    )
    parser.add_argument("--knowledge-mode", choices=("public","system"), default="system",
                        help="Run provenance only; materialization never backdates actual system readiness.")
    parser.add_argument("--statement-timeout-seconds", type=int, default=600)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        raise SystemExit("--since must not be after --until")
    stages = [x.strip() for x in args.stages.split(",") if x.strip()]
    valid = {"research", "events", "supply", "entities", "coverage"}
    unknown = set(stages) - valid
    if unknown:
        raise SystemExit(f"unknown stages: {sorted(unknown)}")

    params = {"since": since, "until": until}
    sql_by_stage = {
        "research": RESEARCH_SQL,
        "events": EVENT_SQL,
        "supply": SUPPLY_SQL,
        "entities": ENTITY_VERSION_SQL,
        "coverage": COVERAGE_SQL,
    }
    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='5s'")
            cur.execute("SELECT set_config('statement_timeout',%s,true)",(str(max(30,args.statement_timeout_seconds)*1000),))
            acquire_graph_write_lock(cur)
            if cur.execute("SELECT to_regclass('kg.version_revision_history') AS relation").fetchone()['relation'] is None:
                raise RuntimeError('install migrate_graph_revision_history_v5.sql before running this builder')
            execute_batch(cur, ENTITY_SQL, params)
            for stage in stages:
                cur.execute(
                    """
                    INSERT INTO kg.temporal_backfill_partitions
                      (stage,source_name,partition_start,partition_end,status,attempt_count,started_at,updated_at)
                    VALUES (%s,'all',%s,%s,'running',1,now(),now())
                    ON CONFLICT (stage,source_name,partition_start,partition_end) DO UPDATE SET
                      status='running',attempt_count=kg.temporal_backfill_partitions.attempt_count+1,
                      started_at=now(),error_text=NULL,updated_at=now()
                    """,
                    (stage, since, until),
                )
                try:
                    written = execute_batch(cur, sql_by_stage[stage], params)
                    if stage == 'supply':
                        cur.execute(RECONCILE_EXPOSURES_SQL)
                        written += max(cur.rowcount,0)
                    cur.execute(
                        """
                        UPDATE kg.temporal_backfill_partitions SET status='complete',rows_written=%s,
                          finished_at=now(),updated_at=now() WHERE stage=%s AND source_name='all'
                          AND partition_start=%s AND partition_end=%s
                        """,
                        (written, stage, since, until),
                    )
                    print(f"stage={stage} written={written}", flush=True)
                except Exception as exc:
                    conn.rollback()
                    with conn.cursor() as err_cur:
                        err_cur.execute(
                            """
                            INSERT INTO kg.temporal_backfill_partitions
                              (stage,source_name,partition_start,partition_end,status,attempt_count,error_text,finished_at,updated_at)
                            VALUES (%s,'all',%s,%s,'failed',1,%s,now(),now())
                            ON CONFLICT (stage,source_name,partition_start,partition_end) DO UPDATE SET
                              status='failed',error_text=EXCLUDED.error_text,finished_at=now(),updated_at=now()
                            """,
                            (stage, since, until, str(exc)[:2000]),
                        )
                        conn.commit()
                    raise
            print("source_review="+str(review_current(conn, apply=True)), flush=True)
            conn.commit()
    print(f"temporal_kg complete since={since} until={until} stages={','.join(stages)} knowledge_mode={args.knowledge_mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
