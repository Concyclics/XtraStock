#!/usr/bin/env python3
"""Freeze source-backed research once, then persist the explainable watchlist."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
from collections import defaultdict
from zoneinfo import ZoneInfo
from pathlib import Path
import sys
import bisect
import math
from common import connect,jsonb
from factor_validation import independent_windows
from research_snapshot import VERSION,WATCHLIST_NAME,select_watchlist
from forecast_baseline import FEATURES,scenario
from snapshot_input_contract import freeze_contract
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'pipeline'))
from market_data_date import complete_market_date

TZ=ZoneInfo('Asia/Shanghai')

def serial(value):
    return json.loads(json.dumps(value,default=str,ensure_ascii=False))

def sha(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()


def factor_eligibility(definition,evaluations,approval,health,as_of,cutoff):
    """Score provenance alone cannot confer time-valid human approval."""
    windows=independent_windows(evaluations,definition['version'])
    latest_passed=bool(evaluations and independent_windows(evaluations[:1],definition['version']))
    decision_date=cutoff.astimezone(TZ).date()
    approval_valid=bool(approval.get('decision')=='approved'
        and str(approval.get('approved_by') or '').strip()
        and approval.get('factor_version')==definition['version']
        and approval.get('created_at') is not None and approval['created_at']<=cutoff
        and approval.get('effective_date') is not None and approval['effective_date']<=decision_date
        and (approval.get('expires_at') is None or approval['expires_at']>cutoff))
    health_visible=bool(health.get('dt')==as_of
        and health.get('computed_at') is not None and health['computed_at']<=cutoff
        and health.get('latest_available_at') is not None and health['latest_available_at']<=cutoff)
    def finite(value):
        return value is not None and math.isfinite(float(value))
    healthy=bool(health_visible and finite(health.get('coverage')) and float(health['coverage'])>=.8
        and finite(health.get('raw_stddev')) and float(health['raw_stddev'])>0
        and finite(health.get('zero_value_ratio')) and float(health['zero_value_ratio'])<.99)
    eligible=bool(len(windows)>=3 and latest_passed and definition['lifecycle_status']=='production'
                  and approval_valid and healthy)
    return {'passing_independent_windows':len(windows),'prediction_eligible':eligible,
            'approval_valid_at_cutoff':approval_valid,'health_visible_at_cutoff':health_visible}


def collect(conn,as_of,cutoff):
    params={'date':as_of,'cutoff':cutoff}
    market=conn.execute("""SELECT a.asset_id,a.symbol stock_code,a.name stock_name,
        b.dt,b.close,b.volume,b.amount,b.source,b.price_basis,b.quality_status,b.available_at
        FROM market.canonical_bars_daily_v2 b JOIN market.assets a USING(asset_id)
        WHERE b.dt=%(date)s AND b.available_at<=%(cutoff)s AND a.asset_type='stock'
        ORDER BY a.symbol""",params).fetchall()
    # Rank a price*volume proxy within each source; do not invent a CNY amount
    # when the provider does not specify whether its volume is shares or lots.
    proxies=defaultdict(list)
    for m in market:
        if m.get('volume') and m.get('close'):
            proxies[m['source']].append(float(m['volume'])*float(m['close']))
    for values in proxies.values(): values.sort()
    for m in market:
        values=proxies[m['source']]
        value=float(m.get('volume') or 0)*float(m.get('close') or 0)
        m['liquidity_proxy_percentile']=bisect.bisect_right(values,value)/len(values) if values and value>0 else None
        m['liquidity_basis']='reported_amount_CNY' if m.get('amount') is not None else 'within_source_price_volume_rank; volume unit not converted'
    definitions=conn.execute("""SELECT * FROM factor.definitions
        WHERE lifecycle_status IN ('shadow','production','degraded') AND updated_at<=%s""",(cutoff,)).fetchall()
    ev=conn.execute("""SELECT e.*,r.test_start,r.test_end,r.horizon_days,r.status
        FROM factor.evaluations e JOIN factor.experiment_runs r USING(run_id)
        WHERE e.sample_name='test' AND e.created_at<=%(cutoff)s AND r.finished_at<=%(cutoff)s AND r.status='complete'
          AND r.horizon_days IN (5,20,60) AND r.test_end<=%(date)s
          AND coalesce(nullif(e.metrics->>'outcome_end','')::date,r.test_end)<=%(date)s
          AND r.config->>'validation_state' IS DISTINCT FROM 'superseded'
        ORDER BY r.finished_at DESC,e.created_at DESC,e.run_id DESC""",params).fetchall()
    approvals={r['factor_id']:r for r in conn.execute("""SELECT DISTINCT ON(factor_id) *
        FROM factor.production_approvals WHERE created_at<=%s
        ORDER BY factor_id,created_at DESC,approval_id DESC""",(cutoff,))}
    health={r['factor_id']:r for r in conn.execute("""SELECT * FROM factor.factor_health_daily
        WHERE dt=%s AND computed_at<=%s AND latest_available_at<=%s""",(as_of,cutoff,cutoff))}
    defs={}
    for d in definitions:
        evaluations=[e for e in ev if e['factor_id']==d['factor_id']]
        h=health.get(d['factor_id'],{}); a=approvals.get(d['factor_id'],{})
        defs[d['factor_id']]={k:d[k] for k in ('factor_name','display_name','family','lifecycle_status','version')}
        defs[d['factor_id']].update(factor_eligibility(d,evaluations,a,h,as_of,cutoff))
    factors=defaultdict(list)
    for s in conn.execute("""WITH cohort AS MATERIALIZED(
        SELECT f.dt,md5(string_agg(f.asset_id::text||':'||extract(epoch FROM f.updated_at)::text,',' ORDER BY f.asset_id)) source_cohort_hash
        FROM factor.feature_daily f JOIN market.assets a USING(asset_id)
        WHERE f.dt=%s AND a.asset_type='stock' AND NOT coalesce(a.metadata ? 'identity_alias_of',false) GROUP BY f.dt)
        SELECT s.* FROM factor.daily_scores s JOIN cohort cv ON cv.dt=s.dt AND cv.source_cohort_hash=s.source_cohort_hash
        JOIN factor.feature_daily f ON f.asset_id=s.asset_id AND f.dt=s.dt
        JOIN factor.definitions d USING(factor_id)
        WHERE s.dt=%s AND s.available_at<=%s AND s.updated_at<=%s AND f.updated_at<=%s
        AND s.factor_id=ANY(%s) AND s.quality_status='valid' AND s.input_contract_version='pit-input-v1'
        AND s.source_feature_updated_at=f.updated_at AND s.source_factor_version=d.version
        AND s.available_at=f.available_at AND f.input_quality->>'strict_pit_ready'='true'
        AND f.knowledge_mode='system' AND f.input_contract_version='pit-input-v1'""",(as_of,as_of,cutoff,cutoff,cutoff,list(defs))):
        factors[s['asset_id']].append({**defs[s['factor_id']],**{k:s[k] for k in ('dt','raw_value','percentile','zscore','available_at',
            'quality_status','input_contract_version','source_feature_updated_at','source_factor_version','source_decision_cutoff','source_cohort_hash')}})
    financials=defaultdict(list)
    for f in conn.execute("""SELECT DISTINCT ON(asset_id,metric_code) asset_id,metric_code,metric_name,
        period_end,value,yoy,unit,available_at,source_name,source_document_id,quality_status
        FROM company.financial_facts WHERE fact_kind='actual' AND available_at<=%s
          AND quality_status NOT IN ('rejected','conflict')
          AND coalesce(metadata->>'revalidation_required','false') <> 'true'
        ORDER BY asset_id,metric_code,period_end DESC,revision_no DESC,available_at DESC""",(cutoff,)):
        financials[f['asset_id']].append(dict(f))
    financial_ratios=defaultdict(list)
    ratio_names={'reported_net_margin':'合并净利率','reported_liabilities_assets':'资产负债率',
        'reported_operating_cash_margin':'经营现金流／营业收入'}
    for f in conn.execute("""SELECT DISTINCT ON(asset_id,feature_name) asset_id,feature_name,feature_value,
        period_start,period_end,period_type,statement_scope,available_at,source_document_ids,quality_status
        FROM factor.financial_feature_snapshots WHERE knowledge_mode='system' AND created_at<=%s
          AND decision_cutoff<=%s AND available_at<=%s AND as_of_date<=%s
        ORDER BY asset_id,feature_name,created_at DESC""",(cutoff,cutoff,cutoff,as_of)):
        financial_ratios[f['asset_id']].append({**dict(f),'display_name':ratio_names[f['feature_name']],
            'unit':'ratio','usage':'已核实财务输入的研究指标；尚未通过收益检验'})
    themes=defaultdict(list)
    for t in conn.execute("""SELECT stock_code,concept_id,relation_type,evidence_count_90d,evidence
        FROM analytics.ai_hardware_theme_scores WHERE as_of_date=%s AND updated_at<=%s
          AND evidence_count_90d>0 ORDER BY score DESC""",(as_of,cutoff)):
        if len(themes[t['stock_code']])<3: themes[t['stock_code']].append(dict(t))
    graph=defaultdict(list)
    for g in conn.execute("""SELECT s.metadata->>'stock_code' source_code,t.metadata->>'stock_code' target_code,
        s.name source_name,t.name target_name,r.relation_type,rv.relation_version_id,
        lower(rv.valid_during) valid_from,upper(rv.valid_during) valid_to,
        lower(rv.public_known_during) public_known_from,rv.review_status,rv.status,rv.confidence,
        rv.source_system,rv.extractor_version,ev.document_id,ev.evidence_text
        FROM kg.relation_versions rv JOIN kg.relations r USING(relation_id)
        JOIN kg.market_entities s ON s.entity_id=r.source_entity_id
        JOIN kg.market_entities t ON t.entity_id=r.target_entity_id
        JOIN LATERAL (SELECT document_id,left(evidence_text,350) evidence_text
            FROM kg.relation_version_evidence WHERE relation_version_id=rv.relation_version_id
              AND system_at<=%(cutoff)s AND public_at<=%(cutoff)s
            ORDER BY public_at DESC LIMIT 1) ev ON true
        WHERE rv.valid_during @> %(date)s::date AND rv.public_known_during @> %(cutoff)s::timestamptz
          AND rv.system_known_during @> %(cutoff)s::timestamptz AND rv.status='active'
          AND rv.review_status IN ('approved','classified')
          AND coalesce(rv.attributes->>'superseded_by_canonical','false') <> 'true'
          AND coalesce(rv.attributes->'attribution_check'->>'supported','true') <> 'false'
          AND (s.metadata ? 'stock_code' OR t.metadata ? 'stock_code')
        ORDER BY rv.confidence DESC""",params):
        g=dict(g);g['claim_class']='observed_fact' if g['review_status']=='approved' and any(x in g['source_system'].lower() for x in ('official','cninfo','sse','szse')) else ('model_inference' if 'llm' in (g['extractor_version'] or '').lower() else 'extracted_claim')
        for code in {g['source_code'],g['target_code']}:
            if code and len(graph[code])<8: graph[code].append(g)
    targets=defaultdict(list)
    for r in conn.execute("""SELECT r.stock_code,r.institution,r.report_date,r.source_url,r.document_id,
        rr.target_price,rr.confidence,rr.metadata FROM research.report_ratings rr JOIN research.reports_resolved r USING(document_id)
        WHERE rr.target_price>0 AND r.available_at IS NOT NULL
          AND greatest(r.available_at,r.identity_known_at)<=%s AND rr.created_at<=%s
          AND r.report_date BETWEEN %s::date-180 AND %s::date ORDER BY r.report_date DESC""",(cutoff,cutoff,as_of,as_of)):
        if len(targets[r['stock_code']])<3:
            terms=r['metadata'] or {}
            expired=bool(terms.get('target_valid_until') and str(terms['target_valid_until'])<str(as_of))
            targets[r['stock_code']].append({**dict(r),'currency':terms.get('target_currency'),
                'currency_status':'原文明示' if terms.get('target_currency') else '原文尚未提取币种',
                'horizon_months':terms.get('target_horizon_months'),'valid_until':terms.get('target_valid_until'),
                'expired':expired,'price_basis':terms.get('target_price_basis','复权口径未知'),
                'horizon_days':None,'expiry_policy':'180天展示窗口及原文明示期限；过期值不可作当前目标',
                'kind':'broker_target','target_space':None})
    models={r['horizon_days']:dict(r) for r in conn.execute("""SELECT DISTINCT ON(horizon_days) *
        FROM analytics.forecast_models WHERE created_at<=%s AND decision_cutoff<=%s
        ORDER BY horizon_days,created_at DESC""",(cutoff,cutoff))}
    feature_rows={str(r['asset_id']):r for r in conn.execute("SELECT asset_id,input_contract_version,decision_cutoff,knowledge_mode,input_quality,updated_at,"+','.join(FEATURES)+
        " FROM factor.feature_daily WHERE dt=%s AND available_at<=%s AND updated_at<=%s",(as_of,cutoff,cutoff))}
    previous={r['stock_code']:r['payload'] for r in conn.execute("""SELECT stock_code,payload FROM analytics.research_snapshot_daily
        WHERE snapshot_id=(SELECT snapshot_id FROM analytics.research_snapshot_runs WHERE status='complete'
            AND as_of_date<%s ORDER BY as_of_date DESC,completed_at DESC LIMIT 1)""",(as_of,))}
    payloads=[]
    for m in market:
        a=m['asset_id'];code=m['stock_code'];f=factors[a];fin=financials[a];warnings=[]
        if m.get('amount') is None: warnings.append('缺少官方成交额；流动性仅用同源价量排名筛选，不换算成人民币成交额')
        if not fin: warnings.append('缺少可用真实财务数据')
        elif max(x['period_end'] for x in fin)<as_of-dt.timedelta(days=210): warnings.append('最新财务报告期已超过210天')
        if len(f)<len(defs): warnings.append(f'因子缺失 {len(defs)-len(f)}/{len(defs)}')
        if not any(x['prediction_eligible'] for x in f): warnings.append('没有通过完整验证并获批准的生产因子；因子仅供研究')
        scenarios=[]
        for horizon in (5,20,60):
            model=models.get(horizon);feature=feature_rows.get(str(a))
            if model and feature:
                x=[feature[k] if feature[k] is not None else float('nan') for k in FEATURES]
                item=scenario(model,x,float(m['close']),any(x['prediction_eligible'] for x in f),as_of=as_of)
                item['model_id']=str(model['model_id'])
            else:
                item={'horizon_trading_days':horizon,'status':'unavailable','prediction_eligible':False,
                      'blockers':['尚无成熟标签验证的模型或当日特征'],'model_id':None}
            scenarios.append(item)
        before={str(g['relation_version_id']) for g in previous.get(code,{}).get('graph',[])}
        after={str(g['relation_version_id']) for g in graph[code]}
        delta={'added_in_displayed_evidence':sorted(after-before),'removed_from_displayed_evidence':sorted(before-after),
               'scope':'Top 8 evidence-backed relations; disappearance can reflect ranking, not necessarily relation invalidation',
               'previous_snapshot_available':code in previous}
        payloads.append(serial({'asset_id':a,'stock_code':code,'stock_name':m['stock_name'],'as_of_date':as_of,
            'decision_cutoff':cutoff,'market':dict(m),'actual_financials':fin,'financial_ratios':financial_ratios[a],
            'input_contract':freeze_contract(feature_rows.get(str(a)),f),
            'factors':f,'expected_factors':len(defs),
            'theme':themes[code],'graph':graph[code],'graph_changes':delta,'scenarios':scenarios,'broker_targets':targets[code],'limitations':warnings}))
    return payloads

def run_paper_stage(as_of):
    """Separate paper status from a successfully frozen research snapshot."""
    import subprocess
    root=Path(__file__).resolve().parents[2]
    result=subprocess.run([str(root/sys.executable),str(root/'scripts/analytics/run_quant_paper_daily.py'),
        '--date',str(as_of)],cwd=root,capture_output=True,text=True,timeout=180)
    folder=root/'logs/quant_framework';folder.mkdir(parents=True,exist_ok=True)
    artifact={'date':str(as_of),'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr[-3000:]}
    (folder/'latest.json').write_text(json.dumps(artifact,ensure_ascii=False,indent=2))
    if result.returncode:raise RuntimeError('paper readiness stage failed; see logs/quant_framework/latest.json')
    print(result.stdout.strip())

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--date');parser.add_argument('--limit-watchlist',type=int,default=10)
    args=parser.parse_args();cutoff=dt.datetime.now(TZ);requested=dt.date.fromisoformat(args.date) if args.date else cutoff.date()
    with connect(autocommit=False) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SET LOCAL statement_timeout='180s'")
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('research-snapshot-v4',0))")
        as_of,quality=complete_market_date(conn,requested,cutoff)
        payloads=collect(conn,as_of,cutoff)
        if not payloads: raise RuntimeError('No eligible market rows; refusing empty snapshot')
        # Content version excludes wall clock so identical inputs do not create duplicate pools.
        stable=[{k:v for k,v in p.items() if k!='decision_cutoff'} for p in payloads]
        fingerprint=sha(stable)
        existing=conn.execute("SELECT snapshot_id FROM analytics.research_snapshot_runs WHERE as_of_date=%s AND version=%s AND input_fingerprint=%s AND status='complete'",(as_of,VERSION,fingerprint)).fetchone()
        if existing:
            if not conn.execute('SELECT 1 FROM agent.watchlist_decisions WHERE snapshot_id=%s AND decision IN (\'added\',\'retained\') LIMIT 1',(existing['snapshot_id'],)).fetchone():
                conn.execute("""UPDATE agent.watchlist_items SET metadata=metadata||%s
                    WHERE watchlist_id=(SELECT watchlist_id FROM agent.watchlists WHERE name=%s) AND removed_at IS NULL""",
                    (jsonb({'qualification_status':'awaiting_revalidation','qualification_checked_date':str(as_of)}),WATCHLIST_NAME))
            conn.commit();print({'reused_snapshot':str(existing['snapshot_id'])});run_paper_stage(as_of);return 0
        sid=conn.execute("""INSERT INTO analytics.research_snapshot_runs(as_of_date,decision_cutoff,version,input_fingerprint,quality)
            VALUES(%s,%s,%s,%s,%s) RETURNING snapshot_id""",(as_of,cutoff,VERSION,fingerprint,jsonb(quality))).fetchone()['snapshot_id']
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO analytics.research_snapshot_daily VALUES(%s,%s,%s,%s,%s)",
                [(sid,p['asset_id'],p['stock_code'],jsonb(p),sha(p)) for p in payloads])
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO analytics.forecast_scenarios(snapshot_id,asset_id,horizon_days,model_id,scenario) VALUES(%s,%s,%s,%s,%s)",
                [(sid,p['asset_id'],s['horizon_trading_days'],s.get('model_id'),jsonb(s)) for p in payloads for s in p['scenarios']])
        wid=conn.execute("""INSERT INTO agent.watchlists(name,description,metadata) VALUES(%s,%s,%s)
            ON CONFLICT(name) DO UPDATE SET updated_at=now() RETURNING watchlist_id""",
            (WATCHLIST_NAME,'因子与图谱支持的研究观察池；非自动交易组合',jsonb({'owner':'research-snapshot-v4'}))).fetchone()['watchlist_id']
        previous={r['stock_code'] for r in conn.execute("SELECT stock_code FROM agent.watchlist_items WHERE watchlist_id=%s AND removed_at IS NULL",(wid,))}
        selected=select_watchlist(payloads,args.limit_watchlist);current=set()
        if quality['stale_sessions']>3: selected=[]
        if not selected:
            quality['watchlist_refresh']='无合格新名单；原观察池保留，但本快照不宣称其仍通过筛选'
            conn.execute('UPDATE analytics.research_snapshot_runs SET quality=%s WHERE snapshot_id=%s',(jsonb(quality),sid))
            conn.execute("""UPDATE agent.watchlist_items SET metadata=metadata||%s
                WHERE watchlist_id=%s AND removed_at IS NULL""",
                (jsonb({'qualification_status':'awaiting_revalidation','qualification_checked_date':str(as_of)}),wid))
        for rank,(score,code,p,reasons,coverage,production) in enumerate(selected,1):
            current.add(code);detail={'reasons':reasons,'score':score,'factor_coverage':coverage,'production_factors':production,
                'invalidation':['行情或财务数据过期','因子覆盖下降','主题证据失效'],'limitations':p['limitations']}
            conn.execute("INSERT INTO agent.watchlist_decisions(snapshot_id,watchlist_id,stock_code,decision,rank,reason,reference_price) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (sid,wid,code,'retained' if code in previous else 'added',rank,jsonb(detail),p['market']['close']))
            conn.execute("""INSERT INTO agent.watchlist_items(watchlist_id,stock_code,thesis,tags,metadata)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(watchlist_id,stock_code) DO UPDATE
                SET thesis=excluded.thesis,tags=excluded.tags,metadata=excluded.metadata,removed_at=NULL""",
                (wid,code,'；'.join(r['text'] for r in reasons),['AI硬件','研究观察'],jsonb({'snapshot_id':str(sid),'rank':rank,'reason':detail,
                    'qualification_status':'research_selected','last_qualified_date':str(as_of),'qualification_checked_date':str(as_of)})))
        for code in (previous-current if selected else set()):
            conn.execute("UPDATE agent.watchlist_items SET removed_at=%s WHERE watchlist_id=%s AND stock_code=%s",(cutoff,wid,code))
            conn.execute("INSERT INTO agent.watchlist_decisions(snapshot_id,watchlist_id,stock_code,decision,reason) VALUES(%s,%s,%s,'removed',%s)",
                (sid,wid,code,jsonb({'text':'未达到当期流动性、数据覆盖、主题证据或排名要求；不等于卖出建议'})))
        conn.execute("UPDATE analytics.research_snapshot_runs SET status='complete',completed_at=clock_timestamp() WHERE snapshot_id=%s",(sid,))
        conn.commit()
        print(json.dumps({'snapshot_id':str(sid),'as_of':str(as_of),'stocks':len(payloads),'watchlist':len(selected),'quality':quality},ensure_ascii=False))
    run_paper_stage(as_of)
    return 0
if __name__=='__main__': raise SystemExit(main())
