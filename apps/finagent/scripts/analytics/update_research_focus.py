#!/usr/bin/env python3
"""Update independent research attention from bounded, read-only source queries."""
from __future__ import annotations
import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import time

try:
    from .common import connect,jsonb
    from .research_focus import VERSION,TZ,serial,fingerprint,build_focus,load_focus,timestamp
    from .research_snapshot import VERSION as SNAPSHOT_VERSION
    from .research_focus_feedback import collect_focus_feedback,feedback_subjects
except ImportError:
    from common import connect,jsonb
    from research_focus import VERSION,TZ,serial,fingerprint,build_focus,load_focus,timestamp
    from research_snapshot import VERSION as SNAPSHOT_VERSION
    from research_focus_feedback import collect_focus_feedback,feedback_subjects

LOCK_ID=704201350
ROOT=Path(__file__).resolve().parents[2]
BAD_DOCUMENT="""NOT EXISTS(SELECT 1 FROM corpus.document_quarantine q WHERE q.document_id=d.id AND q.released_at IS NULL)
 AND NOT EXISTS(SELECT 1 FROM corpus.document_quality q WHERE q.document_id=d.id AND q.quality_status IN ('template_noise','parse_failed','quarantined'))"""


def collect(conn,cutoff,lookback_days=14,as_of=None):
    as_of=as_of or cutoff.astimezone(TZ).date()
    event_cutoff=min(cutoff,dt.datetime.combine(as_of,dt.time.max,TZ))
    params={'cutoff':cutoff,'event_cutoff':event_cutoff,'since':event_cutoff-dt.timedelta(days=lookback_days),
            'graph_since':as_of-dt.timedelta(days=90),'as_of':as_of}
    snapshot=conn.execute('''SELECT snapshot_id,as_of_date,version,quality FROM analytics.research_snapshot_runs
        WHERE status='complete' AND decision_cutoff<=%s AND completed_at<=%s AND as_of_date<=%s AND version=%s
        ORDER BY as_of_date DESC,completed_at DESC LIMIT 1''',(cutoff,cutoff,as_of,SNAPSHOT_VERSION)).fetchone()
    filings=conn.execute('''SELECT f.stock_code,a.name stock_name,f.title,f.source_name,f.source_url url,
        f.announced_at published_at,greatest(f.available_at,f.created_at,f.updated_at,d.created_at,d.updated_at) available_at,
        f.document_id,d.content_hash content_version,f.quality_status,coalesce(q.body_chars,0) body_chars,
        'official_disclosure' kind,1.0::float8 source_quality
        FROM company.filings f JOIN corpus.documents d ON d.id=f.document_id
        JOIN market.assets a ON a.asset_id=f.asset_id LEFT JOIN corpus.document_quality q ON q.document_id=d.id
        WHERE f.quality_status='verified' AND f.announced_at BETWEEN %(since)s AND %(event_cutoff)s
          AND greatest(f.available_at,f.created_at,f.updated_at,d.created_at,d.updated_at)<=%(cutoff)s
          AND NOT coalesce(a.metadata ? 'identity_alias_of',false) AND '''+BAD_DOCUMENT+'''
        ORDER BY f.announced_at DESC,f.stock_code,f.document_id LIMIT 301''',params).fetchall()
    reports=conn.execute('''SELECT r.stock_code,r.stock_name,r.report_title title,r.institution source_name,
        coalesce(r.source_url,d.url) url,d.published_at,
        greatest(r.available_at,r.created_at,r.updated_at,r.identity_known_at,d.created_at,d.updated_at) available_at,
        r.document_id,d.content_hash content_version,coalesce(q.quality_status,'unverified') quality_status,
        coalesce(q.body_chars,0) body_chars,'broker_opinion' kind,.70::float8 source_quality
        FROM research.reports_resolved r JOIN corpus.documents d ON d.id=r.document_id
        LEFT JOIN corpus.document_quality q ON q.document_id=d.id
        WHERE d.published_at BETWEEN %(since)s AND %(event_cutoff)s AND r.stock_code ~ '^[0-9]{6}$'
          AND greatest(r.available_at,r.created_at,r.updated_at,r.identity_known_at,d.created_at,d.updated_at)<=%(cutoff)s AND '''+BAD_DOCUMENT+'''
        ORDER BY d.published_at DESC,r.stock_code,r.document_id LIMIT 121''',params).fetchall()
    graph=conn.execute('''SELECT e.stock_code,e.stock_name,e.concept_id,c.name concept_name,
        e.relation_type,e.status,e.review_status,e.evidence_document_id document_id,d.content_hash,
        coalesce(d.published_at,e.evidence_date::timestamptz) published_at,
        greatest(e.created_at,e.updated_at,d.created_at,d.updated_at) available_at,e.confidence,
        e.source source_name,d.url,e.directness,e.extractor_version,
        coalesce(e.metadata->'attribution_check'->>'supported','false')='true' attribution_supported,
        left(coalesce(e.evidence_span,e.evidence,''),220) evidence_excerpt
        FROM kg.company_concept_exposures e JOIN kg.concepts c USING(concept_id)
        JOIN corpus.documents d ON d.id=e.evidence_document_id
        WHERE e.status='active' AND e.review_status IN ('approved','classified') AND e.confidence>=.75
          AND e.evidence_date>=%(graph_since)s AND e.evidence_date<=%(as_of)s
          AND greatest(e.created_at,e.updated_at,d.created_at,d.updated_at)<=%(cutoff)s
          AND coalesce(e.metadata->'attribution_check'->>'supported','false')='true'
          AND e.relation_type IN ('direct_product','material_supplier','equipment_supplier') AND '''+BAD_DOCUMENT+'''
        ORDER BY e.evidence_date DESC,e.confidence DESC,e.stock_code,e.concept_id LIMIT 1001''',params).fetchall()
    bounded={'filings':len(filings)>300,'reports':len(reports)>120,'graph_relations':len(graph)>1000}
    filings=filings[:300];reports=reports[:120];graph=graph[:1000]
    schema=conn.execute("SELECT to_regclass('analytics.research_focus_runs') present").fetchone()['present']
    previous_run=conn.execute('''SELECT run_id,decision_cutoff FROM analytics.research_focus_runs
        WHERE decision_cutoff<=%s AND created_at<=%s AND as_of_date<=%s
        ORDER BY created_at DESC LIMIT 1''',(cutoff,cutoff,as_of)).fetchone() if schema else None
    previous=conn.execute('SELECT payload FROM analytics.research_focus_revisions WHERE run_id=%s',(previous_run['run_id'],)).fetchall() if previous_run else []
    previous=[r['payload'] for r in previous]
    codes=sorted({str(r['stock_code']) for r in [*filings,*reports,*graph]}|{c for p in previous for c in p['stock_codes']})
    contexts={}
    if snapshot and codes:
        for row in conn.execute('''SELECT stock_code,payload->>'stock_name' stock_name,payload->'market' market,
            payload->'actual_financials' actual_financials,payload->'financial_ratios' financial_ratios,
            jsonb_array_length(payload->'factors') factor_count,payload->>'expected_factors' expected_factors
            FROM analytics.research_snapshot_daily WHERE snapshot_id=%s AND stock_code=ANY(%s)''',(snapshot['snapshot_id'],codes)):
            financials=[f for f in row['actual_financials'] or [] if f.get('quality_status')=='verified']
            fin=sorted(financials,key=lambda f:(str(f.get('period_end')),str(f.get('metric_code'))),reverse=True)[:4]
            market=row['market'] or {};context={'stock_name':row['stock_name'],'snapshot_as_of':str(snapshot['as_of_date']),
                'market':{k:market.get(k) for k in ('dt','close','source','price_basis','quality_status')},
                'financials':[{k:f.get(k) for k in ('metric_code','metric_name','period_end','value','yoy','unit','source_name')} for f in fin],
                'factor_coverage':{'available':row['factor_count'] or 0,'expected':int(row['expected_factors'] or 0)},
                'financial_baseline':{f['metric_code']:{k:f.get(k) for k in ('period_end','value','unit')} for f in fin}}
            contexts[row['stock_code']]=context
    # Review existing subjects first; monitor coverage is explicit and bounded.
    feedback_codes=feedback_subjects(previous)
    feedback,feedback_meta=collect_focus_feedback(conn,cutoff,as_of,feedback_codes)
    for code,value in feedback.items():contexts.setdefault(code,{})['forecast_feedback']=value
    theme_map={};graph_evidence=[]
    for g in graph:
        themes=theme_map.setdefault(g['stock_code'],{})
        themes[str(g['concept_id'])]={'id':str(g['concept_id']),'name':g['concept_name']}
        graph_evidence.append({**g,'kind':'graph_lead','quality_status':'accepted','source_quality':min(.65,float(g['confidence'])),
            'content_version':fingerprint([g['content_hash'],g['relation_type'],g['directness'],g['extractor_version']]),
            'title':f"{g['stock_name'] or g['stock_code']}与{g['concept_name']}的业务关联线索",
            'themes':[{'id':str(g['concept_id']),'name':g['concept_name']}]})
    evidence=[]
    for e in [*filings,*reports]:
        e=dict(e);e['status']='active';e['themes']=list(theme_map.get(e['stock_code'],{}).values())
        e['evidence_detail']='已获取正文，影响分析仍需引用原文' if e['body_chars']>=100 else '目前仅有来源与标题可核对；正文需要补充检索'
        if e['body_chars']<100:e['source_quality']*=.80
        evidence.append(e)
    evidence.extend(graph_evidence)
    return serial(evidence),serial(contexts),previous,{
        'snapshot':serial(snapshot) if snapshot else None,'previous_run':serial(previous_run) if previous_run else None,
        'as_of_date':str(as_of),'knowledge_mode':'current_system_observation' if as_of==cutoff.astimezone(TZ).date() else 'retrospective_using_current_system_knowledge',
        'lookback_days':lookback_days,'source_windows':{'filings_days':lookback_days,'reports_days':lookback_days,'graph_days':90},'limits':{'filings':300,'reports':120,'graph_relations':1000},
        'source_truncated':bounded,'source_counts':{'filings':len(filings),'reports':len(reports),'graph_relations':len(graph)},
        'forecast_feedback':feedback_meta,
        'knowledge_policy':'Only records already ingested/updated by cutoff; publication age controls novelty decay. Active classified graph relations are extracted leads, never verified facts.'}


def update(conn,cutoff,*,apply=False,lookback_days=14,as_of=None):
    if apply and not conn.execute('SELECT pg_try_advisory_xact_lock(%s) locked',(LOCK_ID,)).fetchone()['locked']:
        raise RuntimeError('Another research focus update is running; no source jobs were interrupted')
    as_of=as_of or cutoff.astimezone(TZ).date()
    if apply:
        latest=conn.execute('SELECT as_of_date,decision_cutoff FROM analytics.research_focus_runs ORDER BY created_at DESC LIMIT 1').fetchone()
        if latest and (as_of<latest['as_of_date'] or cutoff<latest['decision_cutoff']):
            raise ValueError('Earlier as-of/cutoff cannot replace current research focus; use a dry run for retrospective analysis')
    evidence,contexts,previous,collection=collect(conn,cutoff,lookback_days,as_of)
    previous_cutoff=timestamp((collection.get('previous_run') or {}).get('decision_cutoff'))
    if apply and previous_cutoff and cutoff<previous_cutoff:
        raise ValueError('Cannot append an earlier decision cutoff after a newer focus run')
    as_of=as_of or cutoff.astimezone(TZ).date()
    # Internal lineage stays in the DB. It is excluded by load_focus's public rows.
    identity=fingerprint({'version':VERSION,'as_of':as_of,'evidence':evidence,'contexts':contexts,
                          'snapshot':(collection.get('snapshot') or {}).get('snapshot_id'),'lookback_days':lookback_days})
    present=conn.execute("SELECT to_regclass('analytics.research_focus_runs') present").fetchone()['present']
    existing=conn.execute('SELECT run_id FROM analytics.research_focus_runs WHERE version=%s AND input_fingerprint=%s',(VERSION,identity)).fetchone() if present else None
    if existing:return {'status':'unchanged','run_id':str(existing['run_id']),'collection':collection,'written':False}
    rows,summary=build_focus(evidence,contexts,previous,cutoff,as_of=as_of)
    summary['collection']=collection;summary['research_available']=any(p['state'] in ('active','watching') for p in rows)
    summary['forecast_feedback']=collection.get('forecast_feedback')
    result={'status':'updated' if apply else 'dry_run','as_of_date':str(as_of),'decision_cutoff':str(cutoff),
            'summary':summary,'rows':rows,'written':False}
    if apply:
        run=conn.execute('''INSERT INTO analytics.research_focus_runs(version,as_of_date,decision_cutoff,input_fingerprint,source_snapshot_id,summary)
            VALUES(%s,%s,%s,%s,%s,%s) RETURNING run_id''',(VERSION,as_of,cutoff,identity,(collection.get('snapshot') or {}).get('snapshot_id'),jsonb(summary))).fetchone()
        for p in rows:
            conn.execute('''INSERT INTO analytics.research_focus_revisions(run_id,focus_key,entity_type,label,stock_codes,state,priority,next_review_at,payload)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(run['run_id'],p['focus_key'],p['entity_type'],p['label'],p['stock_codes'],p['state'],p['priority'],p['next_review_at'],jsonb(p)))
        result['run_id']=str(run['run_id']);result['written']=True
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');p.add_argument('--install-schema',action='store_true')
    p.add_argument('--as-of');p.add_argument('--cutoff');p.add_argument('--lookback-days',type=int,default=14);p.add_argument('--output',type=Path)
    args=p.parse_args();cutoff=timestamp(args.cutoff) if args.cutoff else dt.datetime.now(TZ)
    if cutoff>dt.datetime.now(dt.timezone.utc):p.error('cutoff cannot be in the future')
    as_of=dt.date.fromisoformat(args.as_of) if args.as_of else cutoff.astimezone(TZ).date()
    if as_of>cutoff.astimezone(TZ).date():p.error('as-of cannot be after decision cutoff')
    if not 1<=args.lookback_days<=30:p.error('lookback-days must be 1..30')
    if args.install_schema:
        if not args.apply:p.error('--install-schema requires --apply')
        with connect(admin=True,attempts=1) as c:c.execute((ROOT/'docker/postgres/migrate_research_focus_v1.sql').read_text())
    started=time.monotonic()
    with connect(autocommit=False,attempts=1) as c:
        c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'+('' if args.apply else ' READ ONLY'))
        c.execute("SET LOCAL statement_timeout='25s'");c.execute("SET LOCAL lock_timeout='3s'")
        result=update(c,cutoff,apply=args.apply,lookback_days=args.lookback_days,as_of=as_of)
        if args.apply:c.commit()
        else:c.rollback()
    result['elapsed_s']=round(time.monotonic()-started,3)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,default=str,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('rows',)},ensure_ascii=False,default=str))
    return 0

if __name__=='__main__':raise SystemExit(main())
