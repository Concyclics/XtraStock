#!/usr/bin/env python3
"""Build point-in-time graph and research features from temporal KG versions."""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect
from graph_revision_policy import GRAPH_WRITE_LOCK, GRAPH_CONTRACT_VERSION, acquire_graph_write_lock


SQL = """
WITH params AS (
  SELECT %(as_of)s::date AS as_of,
         %(decision_cutoff)s::timestamptz AS cutoff, %(knowledge_mode)s::text AS knowledge_mode
), companies AS (
  SELECT e.entity_id,a.asset_id,a.symbol
  FROM kg.market_entities e JOIN market.assets a ON a.asset_id=e.source_ref::uuid
  WHERE e.entity_type='company' AND e.source_ref ~* '^[0-9a-f-]{36}$' AND a.asset_type='stock'
), known_versions AS (
 SELECT v.*,NULL::jsonb AS frozen_evidence
 FROM kg.relation_versions v CROSS JOIN params p
 WHERE (v.valid_during @> p.as_of OR lower(v.valid_during) BETWEEN p.as_of-180 AND p.as_of)
   AND CASE p.knowledge_mode WHEN 'system' THEN v.system_known_during @> p.cutoff
       ELSE v.public_known_during @> p.cutoff END
 UNION ALL
 SELECT v.*,h.frozen_evidence FROM kg.version_revision_history h
 CROSS JOIN params p CROSS JOIN LATERAL jsonb_populate_record(NULL::kg.relation_versions,h.frozen_record) v
 WHERE p.knowledge_mode='system' AND h.version_kind='relation' AND h.system_known_during @> p.cutoff
   AND (v.valid_during @> p.as_of OR lower(v.valid_during) BETWEEN p.as_of-180 AND p.as_of)
), canonical AS (
 SELECT DISTINCT ON(r.source_entity_id,r.target_entity_id,r.relation_type,
   CASE WHEN r.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO','PRICE_LINKED_TO','CONTAINS','UPSTREAM_OF','DOWNSTREAM_OF','COMPONENT_OF','USED_IN')
     THEN '' ELSE v.version_key END)
   r.source_entity_id,r.target_entity_id,r.relation_type,v.*,lower(v.valid_during) AS fact_date
 FROM kg.relations r JOIN known_versions v USING(relation_id)
 WHERE v.status='active' AND v.source_system<>'company_concept_exposures'
   AND coalesce(v.attributes->'attribution_check'->>'supported','true')<>'false'
   AND coalesce(v.attributes->>'superseded_by_canonical','false')<>'true'
 ORDER BY r.source_entity_id,r.target_entity_id,r.relation_type,
   CASE WHEN r.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO','PRICE_LINKED_TO','CONTAINS','UPSTREAM_OF','DOWNSTREAM_OF','COMPONENT_OF','USED_IN')
     THEN '' ELSE v.version_key END,
   (v.source_system='kg.company_concept_exposures') DESC,v.confidence DESC,lower(v.system_known_during) DESC,v.relation_version_id
), visible AS (
 SELECT v.* FROM canonical v CROSS JOIN params p WHERE v.valid_during @> p.as_of
), recent AS (
 SELECT v.* FROM canonical v CROSS JOIN params p WHERE v.fact_date BETWEEN p.as_of-89 AND p.as_of
), company_recent AS (
 SELECT co.asset_id,r.* FROM companies co JOIN recent r ON r.source_entity_id=co.entity_id
 UNION ALL SELECT co.asset_id,r.* FROM companies co JOIN recent r ON r.target_entity_id=co.entity_id
), company_visible AS (
 SELECT co.asset_id,v.* FROM companies co JOIN visible v ON v.source_entity_id=co.entity_id
 UNION ALL SELECT co.asset_id,v.* FROM companies co JOIN visible v ON v.target_entity_id=co.entity_id
), coverage AS (
  SELECT CASE WHEN sum(expected_documents)=0 THEN NULL
              ELSE least(1.0,sum(greatest(rules_processed,llm_processed))::double precision/sum(expected_documents)) END AS ratio
  FROM kg.source_coverage_daily c CROSS JOIN params p WHERE c.dt BETWEEN p.as_of-29 AND p.as_of
    AND (p.knowledge_mode='public' OR c.updated_at<=p.cutoff)
), exposure AS (
  SELECT co.asset_id,
         count(*) FILTER (WHERE v.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO','PRICE_LINKED_TO'))::int AS exposure_count,
         sum(v.confidence*coalesce(v.weight,1)*CASE coalesce(v.directness,'unknown') WHEN 'direct' THEN 1 WHEN 'indirect' THEN .5 ELSE .7 END)
           FILTER (WHERE v.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO','PRICE_LINKED_TO')) AS direct_score,
         avg(least(1.0,greatest(0.0,(p.as_of-v.fact_date)::double precision/365.0)))
           FILTER (WHERE v.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO')) AS stability
  FROM companies co CROSS JOIN params p LEFT JOIN visible v ON v.source_entity_id=co.entity_id
  GROUP BY co.asset_id
), event_scores AS (
  SELECT co.asset_id,
    sum(r.confidence*coalesce(r.weight,1)) FILTER (WHERE r.relation_type='AFFECTED_BY_EVENT' AND r.direction>0 AND r.fact_date>=p.as_of-29) AS pos,
    sum(abs(r.confidence*coalesce(r.weight,1))) FILTER (WHERE r.relation_type='AFFECTED_BY_EVENT' AND r.direction<0 AND r.fact_date>=p.as_of-29) AS neg,
    sum(r.direction*r.confidence*coalesce(r.weight,1)) FILTER (WHERE r.attributes->>'event_type' IN ('policy_support','government_investment','subsidy') AND r.fact_date>=p.as_of-29) AS policy,
    sum(r.direction*r.confidence*coalesce(r.weight,1)) FILTER (WHERE r.attributes->>'event_type' IN ('demand_growth','demand_decline') AND r.fact_date>=p.as_of-29) AS demand,
    sum(r.direction*r.confidence*coalesce(r.weight,1)) FILTER (WHERE r.attributes->>'event_type'='order_contract' AND r.fact_date>=p.as_of-29) AS orders,
    sum(r.direction*r.confidence*coalesce(r.weight,1)) FILTER (WHERE r.attributes->>'event_type' IN ('price_hike','price_cut') AND r.fact_date>=p.as_of-29) AS pricing,
    sum(abs(r.confidence*coalesce(r.weight,1))) FILTER (WHERE r.attributes->>'event_type' IN ('regulation_risk','external_risk') AND r.fact_date>=p.as_of-29) AS risk,
    count(DISTINCT r.relation_version_id) FILTER (WHERE r.fact_date>=p.as_of-29)::int AS new_relations
  FROM companies co CROSS JOIN params p
  LEFT JOIN company_recent r ON r.asset_id=co.asset_id
  GROUP BY co.asset_id
), event_evidence AS (
 SELECT co.asset_id,count(DISTINCT ev.document_id)::int AS evidence_docs,
   count(DISTINCT coalesce(d.source_name,r.source_system)) FILTER (WHERE r.relation_type='AFFECTED_BY_EVENT')::double precision AS source_diversity
 FROM companies co CROSS JOIN params p JOIN company_recent r
   ON r.asset_id=co.asset_id AND r.fact_date>=p.as_of-29
 LEFT JOIN LATERAL (
   SELECT e.document_id,e.public_at,e.system_at FROM kg.relation_version_evidence e
     WHERE e.relation_version_id=r.relation_version_id AND r.frozen_evidence IS NULL
   UNION ALL SELECT e.document_id,e.public_at,e.system_at
     FROM jsonb_to_recordset(coalesce(r.frozen_evidence,'[]'::jsonb)) AS e(document_id uuid,public_at timestamptz,system_at timestamptz)
 ) ev ON CASE p.knowledge_mode WHEN 'system' THEN ev.system_at<=p.cutoff ELSE ev.public_at<=p.cutoff END
 LEFT JOIN corpus.documents d ON d.id=ev.document_id GROUP BY co.asset_id
), event_features AS (
 SELECT s.*,coalesce(e.evidence_docs,0) AS evidence_docs,coalesce(e.source_diversity,0) AS source_diversity
 FROM event_scores s LEFT JOIN event_evidence e USING(asset_id)
), ratings AS (
  SELECT co.asset_id,
    sum((CASE
      WHEN r.attributes->>'rating_change' ~ '(上调|调高)' THEN 1.5
      WHEN r.attributes->>'rating_change' ~ '(下调|降低)' THEN -1.5
      WHEN r.attributes->>'rating' ~ '(买入|强烈推荐|推荐)' THEN 1
      WHEN r.attributes->>'rating' ~ '(增持)' THEN .5
      WHEN r.attributes->>'rating' ~ '(卖出|减持|回避)' THEN -1 ELSE 0 END)
      * r.confidence * exp(-greatest(0,p.as_of-r.fact_date)/45.0))
      FILTER (WHERE r.relation_type='RATES' AND r.fact_date>=p.as_of-59) AS rating_momentum,
    count(DISTINCT r.source_entity_id) FILTER (WHERE r.relation_type='COVERS_COMPANY' AND r.fact_date>=p.as_of-29)::int AS institutions,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY (r.attributes->>'target_price')::double precision)
      FILTER (WHERE r.relation_type='RATES' AND r.attributes->>'target_price' ~ '^[0-9]+(\\.[0-9]+)?$') AS target_price
  FROM companies co CROSS JOIN params p
  LEFT JOIN recent r ON r.target_entity_id=co.entity_id
  GROUP BY co.asset_id
), forecast_rows AS (
 SELECT co.asset_id,v.fact_date AS report_date,v.attributes->>'institution' AS institution,
   v.attributes->>'document_id' AS document_id,f.forecast_year,f.eps,f.pe
 FROM canonical v JOIN companies co ON co.entity_id=v.target_entity_id CROSS JOIN params p
 CROSS JOIN LATERAL jsonb_to_recordset(coalesce(v.attributes->'forecasts','[]'::jsonb))
   AS f(forecast_year integer,eps double precision,pe double precision,semantic_contract text,year_verified boolean,eps_unit text)
 WHERE v.relation_type='FORECASTS' AND v.fact_date BETWEEN p.as_of-180 AND p.as_of
   AND f.forecast_year>=extract(year FROM p.as_of)::int
   AND f.semantic_contract='earnings-forecast-v2' AND f.year_verified AND f.eps_unit='CNY/share'
), forecast_latest AS (
 SELECT DISTINCT ON(asset_id,institution,forecast_year) fr.* FROM forecast_rows fr
 WHERE nullif(institution,'') IS NOT NULL
 ORDER BY asset_id,institution,forecast_year,report_date DESC,document_id
), forecast_pairs AS (
 SELECT cur.*,nxt.eps AS next_eps,old.eps AS prior_eps,old.report_date AS prior_date,
  sf.split_factor AS current_split,ofac.split_factor AS prior_split
 FROM forecast_latest cur CROSS JOIN params p
 LEFT JOIN forecast_rows nxt ON nxt.asset_id=cur.asset_id AND nxt.document_id=cur.document_id AND nxt.forecast_year=cur.forecast_year+1
 LEFT JOIN LATERAL (
  SELECT x.eps,x.report_date FROM forecast_rows x
  WHERE x.asset_id=cur.asset_id AND x.institution=cur.institution AND x.forecast_year=cur.forecast_year
    AND x.report_date<cur.report_date AND x.report_date>=cur.report_date-90 AND x.eps>0
  ORDER BY x.report_date DESC,x.document_id LIMIT 1
 ) old ON true
 LEFT JOIN LATERAL (SELECT split_factor FROM market.adjustment_factors_daily a
   WHERE a.asset_id=cur.asset_id AND a.dt<=cur.report_date ORDER BY a.dt DESC LIMIT 1) sf ON true
 LEFT JOIN LATERAL (SELECT split_factor FROM market.adjustment_factors_daily a
   WHERE a.asset_id=cur.asset_id AND a.dt<=old.report_date ORDER BY a.dt DESC LIMIT 1) ofac ON true
 WHERE cur.forecast_year=extract(year FROM p.as_of)::int
), forecast_features AS (
 SELECT asset_id,avg(CASE WHEN eps>0 THEN next_eps/eps-1 END) AS eps_growth,
 NULL::double precision AS revenue_growth,avg(pe) FILTER(WHERE pe>0) AS forward_pe,
 avg(CASE WHEN prior_eps>0 AND current_split=prior_split AND current_split>0
  AND report_date>=p.as_of-60 THEN eps/prior_eps-1 END) AS eps_revision,
 NULL::double precision AS revenue_revision,stddev_samp(eps)/nullif(abs(avg(eps)),0) AS dispersion
 FROM forecast_pairs CROSS JOIN params p GROUP BY asset_id
), two_hop AS (
  SELECT co.asset_id,count(DISTINCT v2.target_entity_id)::double precision AS score
  FROM companies co JOIN visible v1 ON v1.source_entity_id=co.entity_id
    AND v1.relation_type IN ('EXPOSED_TO','PRODUCES','USES','SUPPLIES_TO')
  JOIN visible v2 ON v2.source_entity_id=v1.target_entity_id
    AND v2.relation_type IN ('CONTAINS','UPSTREAM_OF','DOWNSTREAM_OF','USED_IN','COMPONENT_OF')
  GROUP BY co.asset_id
), degree AS (
  SELECT co.asset_id,ln(1+count(DISTINCT v.relation_version_id)) AS centrality,
         ln(1+count(DISTINCT ev.document_id))+ln(1+count(DISTINCT v.source_system)) AS evidence_diversity
  FROM companies co CROSS JOIN params p LEFT JOIN company_visible v ON v.asset_id=co.asset_id
  LEFT JOIN LATERAL (
   SELECT e.document_id,e.public_at,e.system_at FROM kg.relation_version_evidence e
   WHERE e.relation_version_id=v.relation_version_id AND v.frozen_evidence IS NULL
   UNION ALL SELECT e.document_id,e.public_at,e.system_at FROM jsonb_to_recordset(coalesce(v.frozen_evidence,'[]'::jsonb)) AS e(document_id uuid,public_at timestamptz,system_at timestamptz)
 ) ev ON CASE p.knowledge_mode WHEN 'system' THEN ev.system_at<=p.cutoff ELSE ev.public_at<=p.cutoff END
  GROUP BY co.asset_id
), price AS (
  SELECT t.asset_id,t.close
  FROM analytics.asset_technical_daily t CROSS JOIN params p
  WHERE t.dt=p.as_of
    AND (p.knowledge_mode='public' OR greatest(t.available_at,t.updated_at)<=p.cutoff)
), final AS (
  SELECT co.asset_id,p.as_of AS dt,coalesce(ex.direct_score,0) AS direct_exposure_score,
    coalesce(ex.exposure_count,0) AS exposure_count,coalesce(ef.pos,0) AS positive_event_score_30d,
    coalesce(ef.neg,0) AS negative_event_score_30d,coalesce(rt.institutions,0) AS institution_coverage_30d,
    coalesce(ef.evidence_docs,0) AS evidence_document_count_30d,coalesce(ef.new_relations,0) AS new_relation_count_30d,
    ln(1+coalesce(ef.new_relations,0))*ln(1+coalesce(ef.evidence_docs,0)) AS novelty_score_30d,
    ln(1+coalesce(ex.direct_score,0))+coalesce(ef.pos,0)-coalesce(ef.neg,0)+.2*ln(1+coalesce(rt.institutions,0)) AS graph_score,
    CASE WHEN cv.ratio>=.9 THEN 'complete' WHEN cv.ratio>0 THEN 'partial' ELSE 'unknown' END AS coverage_status,
    cv.ratio AS source_coverage_ratio,rt.rating_momentum,
    ff.eps_growth,ff.revenue_growth,ff.eps_revision,ff.revenue_revision,
    CASE WHEN ff.forward_pe>0 THEN 1/ff.forward_pe END AS earnings_yield,ff.dispersion,
    CASE WHEN pr.close>0 THEN rt.target_price/pr.close-1 END AS target_upside,
    ef.policy,ef.demand,ef.orders,ef.pricing,ef.risk,ef.source_diversity,
    th.score,dg.centrality,dg.evidence_diversity,ex.stability,p.cutoff AS available_at,
    jsonb_build_object('knowledge_mode',p.knowledge_mode,'known_cutoff',p.cutoff,'decision_cutoff',p.cutoff,
      'forecast_semantics','earnings-forecast-v2','eps_unit','CNY/share','pe_price_basis','source_report_date','eps_revision_method','matched_institution_fiscal_year_split_checked','temporal_schema','v5','input_contract_version','graph-pit-v1','feature_computed_at',clock_timestamp(),
      'strict_pit_ready',p.knowledge_mode='system' AND p.cutoff>=(SELECT installed_at FROM kg.version_history_contract WHERE contract_key='revision-v5'),'coverage_ratio',cv.ratio) AS provenance
  FROM companies co CROSS JOIN params p CROSS JOIN coverage cv
  LEFT JOIN exposure ex ON ex.asset_id=co.asset_id LEFT JOIN event_features ef ON ef.asset_id=co.asset_id
  LEFT JOIN ratings rt ON rt.asset_id=co.asset_id LEFT JOIN forecast_features ff ON ff.asset_id=co.asset_id
  LEFT JOIN two_hop th ON th.asset_id=co.asset_id LEFT JOIN degree dg ON dg.asset_id=co.asset_id
  LEFT JOIN price pr ON pr.asset_id=co.asset_id
)
INSERT INTO analytics.graph_feature_daily (
 asset_id,dt,direct_exposure_score,exposure_count,positive_event_score_30d,negative_event_score_30d,
 institution_coverage_30d,evidence_document_count_30d,new_relation_count_30d,novelty_score_30d,graph_score,
 coverage_status,source_coverage_ratio,rating_momentum_60d,forecast_eps_growth,forecast_revenue_growth,
 forecast_eps_revision_90d,forecast_revenue_revision_90d,forward_earnings_yield,forecast_dispersion_180d,
 target_price_upside,policy_event_impulse_30d,demand_event_impulse_30d,order_event_impulse_30d,
 pricing_event_impulse_30d,risk_event_impulse_30d,event_source_diversity_30d,graph_two_hop_exposure,
 graph_centrality,graph_evidence_diversity,graph_relation_stability,available_at,provenance,updated_at)
SELECT asset_id,dt,direct_exposure_score,exposure_count,positive_event_score_30d,negative_event_score_30d,
 institution_coverage_30d,evidence_document_count_30d,new_relation_count_30d,novelty_score_30d,graph_score,
 coverage_status,source_coverage_ratio,rating_momentum,eps_growth,revenue_growth,eps_revision,revenue_revision,
 earnings_yield,dispersion,target_upside,policy,demand,orders,pricing,risk,source_diversity,score,centrality,
 evidence_diversity,stability,available_at,provenance,now() FROM final
ON CONFLICT (asset_id,dt) DO UPDATE SET
 direct_exposure_score=EXCLUDED.direct_exposure_score,exposure_count=EXCLUDED.exposure_count,
 positive_event_score_30d=EXCLUDED.positive_event_score_30d,negative_event_score_30d=EXCLUDED.negative_event_score_30d,
 institution_coverage_30d=EXCLUDED.institution_coverage_30d,evidence_document_count_30d=EXCLUDED.evidence_document_count_30d,
 new_relation_count_30d=EXCLUDED.new_relation_count_30d,novelty_score_30d=EXCLUDED.novelty_score_30d,
 graph_score=EXCLUDED.graph_score,coverage_status=EXCLUDED.coverage_status,source_coverage_ratio=EXCLUDED.source_coverage_ratio,
 rating_momentum_60d=EXCLUDED.rating_momentum_60d,forecast_eps_growth=EXCLUDED.forecast_eps_growth,
 forecast_revenue_growth=EXCLUDED.forecast_revenue_growth,forecast_eps_revision_90d=EXCLUDED.forecast_eps_revision_90d,
 forecast_revenue_revision_90d=EXCLUDED.forecast_revenue_revision_90d,forward_earnings_yield=EXCLUDED.forward_earnings_yield,
 forecast_dispersion_180d=EXCLUDED.forecast_dispersion_180d,target_price_upside=EXCLUDED.target_price_upside,
 policy_event_impulse_30d=EXCLUDED.policy_event_impulse_30d,demand_event_impulse_30d=EXCLUDED.demand_event_impulse_30d,
 order_event_impulse_30d=EXCLUDED.order_event_impulse_30d,pricing_event_impulse_30d=EXCLUDED.pricing_event_impulse_30d,
 risk_event_impulse_30d=EXCLUDED.risk_event_impulse_30d,event_source_diversity_30d=EXCLUDED.event_source_diversity_30d,
 graph_two_hop_exposure=EXCLUDED.graph_two_hop_exposure,graph_centrality=EXCLUDED.graph_centrality,
 graph_evidence_diversity=EXCLUDED.graph_evidence_diversity,graph_relation_stability=EXCLUDED.graph_relation_stability,
 available_at=EXCLUDED.available_at,provenance=EXCLUDED.provenance,updated_at=now()
"""


def resolve_decision_cutoff(as_of: dt.date, value: str | None = None, *, now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.timezone.utc)
    if value:
        cutoff = dt.datetime.fromisoformat(value.replace('Z','+00:00'))
        if cutoff.tzinfo is None:
            raise ValueError('decision-cutoff must include a timezone')
        if cutoff > now:
            raise ValueError('decision-cutoff cannot be in the future')
        return cutoff
    day_end = dt.datetime.combine(as_of,dt.time(23,59,59),dt.timezone(dt.timedelta(hours=8)))
    return min(now,day_end)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--as-of')
    parser.add_argument('--since')
    parser.add_argument('--until')
    parser.add_argument('--knowledge-mode',choices=('public','system'),default='system')
    parser.add_argument('--decision-cutoff',help='Timezone-aware timestamp; defaults to min(now, Beijing day-end).')
    parser.add_argument('--statement-timeout-seconds',type=int,default=300)
    args = parser.parse_args()
    if args.as_of:
        since = until = dt.date.fromisoformat(args.as_of)
        if since > dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date():
            parser.error('as-of cannot be a future Beijing date')
    elif args.since and args.until:
        since, until = dt.date.fromisoformat(args.since),dt.date.fromisoformat(args.until)
    else:
        parser.error('use --as-of or both --since and --until')
    with connect(autocommit=False) as conn:
        dates = [row['dt'] for row in conn.execute(
            'SELECT DISTINCT dt FROM analytics.asset_technical_daily WHERE dt BETWEEN %s AND %s ORDER BY dt',
            (since,until)).fetchall()]
        # An explicit current snapshot can refresh graph evidence before market
        # bars arrive. Price-dependent fields remain NULL until that day's close.
        if args.as_of:
            dates = [since]
        conn.commit()
        for as_of in dates:
            cutoff = resolve_decision_cutoff(as_of,args.decision_cutoff)
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout='5s'")
                    cur.execute("SELECT set_config('statement_timeout',%s,true)",(str(max(30,args.statement_timeout_seconds)*1000),))
                    acquire_graph_write_lock(cur)
                    cur.execute(SQL,{'as_of':as_of,'decision_cutoff':cutoff,'knowledge_mode':args.knowledge_mode})
                    print(f'as_of={as_of} mode={args.knowledge_mode} decision_cutoff={cutoff.isoformat()} rows={cur.rowcount}',flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
