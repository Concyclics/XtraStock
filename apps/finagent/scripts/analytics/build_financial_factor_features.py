#!/usr/bin/env python3
"""Snapshot three audited financial ratios without changing daily shadow factors."""
from __future__ import annotations
import argparse
import datetime as dt
from decimal import Decimal
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo
try:
    from .common import connect,jsonb
except ImportError:
    from common import connect,jsonb

CONTRACT='financial-ratios-v1'
SPECS={
    'reported_net_margin':('net_profit_total','revenue',False),
    'reported_liabilities_assets':('total_liabilities','total_assets',True),
    'reported_operating_cash_margin':('operating_cash_flow','revenue',False),
}


def ratio_value(name,numerator,denominator,cutoff,knowledge_mode='system'):
    """Refuse mismatched scope, period, source edition, units or unverified facts."""
    ncode,dcode,point=SPECS[name]
    for fact,code in ((numerator,ncode),(denominator,dcode)):
        if fact.get('metric_code')!=code or fact.get('quality_status')!='verified' or fact.get('fact_kind')!='actual':
            return None
        if fact.get('statement_scope')!='consolidated' or fact.get('unit')!='CNY' or fact.get('currency')!='CNY':
            return None
        if fact.get('available_at') is None or fact['available_at']>cutoff:return None
        if knowledge_mode=='system' and (fact.get('created_at') is None or fact['created_at']>cutoff):return None
    for key in ('asset_id','period_start','period_end','period_type','source_document_id','filing_id','revision_no'):
        if numerator.get(key)!=denominator.get(key):return None
    if not numerator.get('source_document_id') or not numerator.get('filing_id') or not numerator.get('period_end'):return None
    if point:
        if numerator['period_type']!='point_in_time':return None
    elif numerator['period_type'] not in ('annual','year_to_date','quarter') or not numerator.get('period_start'):
        return None
    if numerator['period_end']>cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date():return None
    try:
        n,d=Decimal(numerator['value']),Decimal(denominator['value'])
        if not n.is_finite() or not d.is_finite() or d<=0:return None
        return float(n/d)
    except (TypeError,ValueError,ArithmeticError):return None


SELECT_SQL="""
WITH visible AS (
 SELECT f.*,CASE WHEN %(mode)s='system' THEN greatest(f.available_at,f.created_at,d.created_at,d.updated_at)
                 ELSE f.available_at END AS known_at
 FROM company.financial_facts f JOIN market.assets a USING(asset_id)
 JOIN corpus.documents d ON d.id=f.source_document_id
 WHERE f.metric_code=ANY(%(metrics)s) AND f.quality_status='verified' AND f.fact_kind='actual'
   AND f.statement_scope='consolidated' AND f.unit='CNY' AND f.currency='CNY'
   AND f.value IS NOT NULL AND f.period_end BETWEEN %(asof)s::date-%(max_age)s AND %(asof)s
   AND a.asset_type='stock' AND NOT (a.metadata ? 'identity_alias_of')
   AND (%(stock_code)s::text IS NULL OR a.symbol=%(stock_code)s)
), specs(feature_name,ncode,dcode,is_point) AS (VALUES
 ('reported_net_margin','net_profit_total','revenue',false),
 ('reported_liabilities_assets','total_liabilities','total_assets',true),
 ('reported_operating_cash_margin','operating_cash_flow','revenue',false)
), pairs AS (
 SELECT s.feature_name,n.asset_id,n.period_start,n.period_end,n.period_type,n.statement_scope,
   greatest(n.known_at,d.known_at) available_at,to_jsonb(n) numerator,to_jsonb(d) denominator
 FROM specs s JOIN visible n ON n.metric_code=s.ncode JOIN visible d ON d.metric_code=s.dcode
   AND d.asset_id=n.asset_id AND d.period_end=n.period_end
   AND d.period_start IS NOT DISTINCT FROM n.period_start AND d.period_type=n.period_type
   AND d.source_document_id=n.source_document_id AND d.filing_id=n.filing_id AND d.revision_no=n.revision_no
 WHERE n.known_at<=%(cutoff)s AND d.known_at<=%(cutoff)s
   AND d.value>0 AND n.value::text NOT IN ('NaN','Infinity','-Infinity')
   AND d.value::text NOT IN ('NaN','Infinity','-Infinity')
   AND ((s.is_point AND n.period_type='point_in_time') OR
        (NOT s.is_point AND n.period_type IN ('annual','year_to_date','quarter') AND n.period_start IS NOT NULL))
)
SELECT DISTINCT ON(asset_id,feature_name) * FROM pairs
ORDER BY asset_id,feature_name,period_end DESC,available_at DESC,(numerator->>'financial_fact_id')::bigint DESC
"""


def decode_fact(f):
    f=dict(f)
    for k in ('available_at','created_at'):
        if isinstance(f.get(k),str):f[k]=dt.datetime.fromisoformat(f[k])
    for k in ('period_start','period_end'):
        if isinstance(f.get(k),str):f[k]=dt.date.fromisoformat(f[k])
    return f


def main():
    p=argparse.ArgumentParser();p.add_argument('--as-of',required=True)
    p.add_argument('--decision-cutoff');p.add_argument('--knowledge-mode',choices=('system','public'),default='system')
    p.add_argument('--stock-code');p.add_argument('--max-age-days',type=int,default=550);p.add_argument('--dry-run',action='store_true')
    a=p.parse_args();asof=dt.date.fromisoformat(a.as_of);now=dt.datetime.now(dt.timezone.utc)
    cutoff=dt.datetime.fromisoformat(a.decision_cutoff) if a.decision_cutoff else min(now,dt.datetime.combine(asof,dt.time(23,59,59),ZoneInfo('Asia/Shanghai')))
    if cutoff.tzinfo is None or cutoff>now:p.error('cutoff must be timezone-aware and not in the future')
    if a.knowledge_mode=='system' and asof!=now.astimezone(ZoneInfo('Asia/Shanghai')).date():
        p.error('financial system snapshots start from today; historical reruns require explicit public reconstruction')
    if a.max_age_days<1:p.error('max-age-days must be positive')
    args={'asof':asof,'cutoff':cutoff,'mode':a.knowledge_mode,'max_age':a.max_age_days,'stock_code':a.stock_code,
          'metrics':sorted({code for spec in SPECS.values() for code in spec[:2]})}
    snapshot=uuid.uuid4();counts={name:0 for name in SPECS};rejected=0
    with connect(autocommit=False,attempts=1) as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        if a.dry_run:c.execute('SET TRANSACTION READ ONLY')
        rows=c.execute(SELECT_SQL,args).fetchall()
        for row in rows:
            n,d=decode_fact(row['numerator']),decode_fact(row['denominator'])
            value=ratio_value(row['feature_name'],n,d,cutoff,a.knowledge_mode)
            if value is None:rejected+=1;continue
            counts[row['feature_name']]+=1
            if a.dry_run:continue
            inputs={key:{k:f.get(k) for k in ('financial_fact_id','metric_code','value','unit','currency','period_start','period_end','period_type','statement_scope','revision_no','source_document_id','available_at','created_at')}
                    for key,f in (('numerator',n),('denominator',d))}
            digest=hashlib.sha256(json.dumps(inputs,sort_keys=True,default=str).encode()).hexdigest()
            c.execute("""INSERT INTO factor.financial_feature_snapshots(snapshot_id,asset_id,as_of_date,decision_cutoff,knowledge_mode,
              feature_name,feature_value,period_start,period_end,period_type,statement_scope,available_at,
              source_fact_ids,source_document_ids,input_contract_version,metadata)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              (snapshot,row['asset_id'],asof,cutoff,a.knowledge_mode,row['feature_name'],value,row['period_start'],row['period_end'],
               row['period_type'],row['statement_scope'],row['available_at'],[n['financial_fact_id'],d['financial_fact_id']],
               [uuid.UUID(n['source_document_id'])],CONTRACT,jsonb({'input_sha256':digest,'inputs':json.loads(json.dumps(inputs,default=str)),
                 'lifecycle':'research_only','no_ttm_imputation':True,'historical_system_replay_supported':False})))
        if a.dry_run:c.rollback()
        else:c.commit()
    print(json.dumps({'snapshot_id':str(snapshot),'as_of':str(asof),'cutoff':str(cutoff),'knowledge_mode':a.knowledge_mode,
        'counts':counts,'rejected_pairs':rejected,'dry_run':a.dry_run,'status':'prototype_research_only'},ensure_ascii=False))
    return 0
if __name__=='__main__':raise SystemExit(main())
