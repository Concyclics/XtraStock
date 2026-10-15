#!/usr/bin/env python3
"""Update the bounded research pool in an append-only transaction."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

if str(Path(__file__).resolve().parents[2]) not in sys.path:sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from scripts.analytics.common import connect,jsonb
from scripts.analytics.candidate_pool import VERSION,TZ,config,score_candidate,evolve
from scripts.analytics.research_focus_feedback import collect_focus_feedback

ROOT=Path(__file__).resolve().parents[2]
LOCK=704201351

def collect(conn,cutoff,cfg):
    run=conn.execute('''SELECT snapshot_id,as_of_date,completed_at FROM analytics.research_snapshot_runs
        WHERE status='complete' AND version='research-snapshot-v5' AND completed_at<=%s AND decision_cutoff<=%s
        ORDER BY as_of_date DESC,completed_at DESC LIMIT 1''',(cutoff,cutoff)).fetchone()
    if not run:raise ValueError('No completed current-contract research snapshot')
    stale=conn.execute('''SELECT count(*) n FROM market.trading_calendar WHERE exchange='SSE' AND is_verified
        AND session_status='open' AND trade_date>%s AND close_at<=%s''',(run['as_of_date'],cutoff)).fetchone()['n']
    if stale>2:raise ValueError('Research snapshot is more than two completed sessions old')
    source=conn.execute('''SELECT s.stock_code,payload->>'stock_name' stock_name,
        payload->'market' market,payload->'actual_financials' actual_financials,payload->'factors' factors,
        payload->'graph' graph,payload->'theme' theme,a.industry_name,a.lifecycle_status
        FROM analytics.research_snapshot_daily s JOIN market.assets a ON a.asset_id=s.asset_id
        WHERE snapshot_id=%s AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
        ORDER BY s.stock_code''',(run['snapshot_id'],)).fetchall()
    today=cutoff.astimezone(TZ).date()
    industries={r['stock_code']:r for r in conn.execute('''SELECT DISTINCT ON(stock_code)
        stock_code,industry_name,report_date FROM research.reports_resolved
        WHERE stock_code ~ '^[0-9]{6}$' AND nullif(btrim(industry_name),'') IS NOT NULL
          AND report_date BETWEEN %s AND %s AND greatest(available_at,created_at,updated_at,identity_known_at)<=%s
        ORDER BY stock_code,report_date DESC,available_at DESC,document_id''',(today-dt.timedelta(days=730),today,cutoff))}
    # Bound each issuer before the global result limit, so repeated administrative
    # announcements cannot crowd out every other issuer.
    filings=conn.execute('''WITH ranked AS (
        SELECT f.stock_code,f.title,f.source_url url,f.announced_at published_at,f.document_id,
          row_number() OVER(PARTITION BY f.stock_code ORDER BY f.announced_at DESC,f.document_id) rn
        FROM company.filings f JOIN corpus.documents d ON d.id=f.document_id
        WHERE f.quality_status='verified' AND f.announced_at BETWEEN %s AND %s
          AND greatest(f.available_at,f.created_at,f.updated_at,d.created_at,d.updated_at)<=%s
          AND NOT EXISTS(SELECT 1 FROM corpus.document_quarantine q WHERE q.document_id=d.id AND q.released_at IS NULL)
          AND NOT EXISTS(SELECT 1 FROM corpus.document_quality q WHERE q.document_id=d.id
              AND q.quality_status IN ('template_noise','parse_failed','quarantined')))
        SELECT stock_code,title,url,published_at,document_id FROM ranked WHERE rn<=%s
        ORDER BY rn,published_at DESC,stock_code,document_id LIMIT %s''',
        (cutoff-dt.timedelta(days=cfg['filing_lookback_days']),cutoff,cutoff,cfg['filings_per_company'],cfg['filing_limit']+1)).fetchall()
    truncated=len(filings)>cfg['filing_limit'];filings=filings[:cfg['filing_limit']]
    by_code={}
    for f in filings:by_code.setdefault(f['stock_code'],[]).append({k:str(v) if isinstance(v,(dt.date,dt.datetime)) else v for k,v in f.items() if k!='document_id'})
    previous_run=conn.execute('SELECT * FROM analytics.candidate_pool_runs ORDER BY created_at DESC LIMIT 1').fetchone()
    previous=[r['payload'] for r in conn.execute('SELECT payload FROM analytics.candidate_pool_revisions WHERE run_id=%s',
        (previous_run['run_id'],)).fetchall()] if previous_run else []
    if previous_run and previous_run['decision_cutoff']>cutoff:raise ValueError('Cannot backdate a candidate pool update')
    ordered=sorted(previous,key=lambda p:p['rank']);feedback={};feedback_meta=[]
    codes=[p['stock_code'] for p in ordered if p['state'] in ('core','watching')]
    for offset in range(0,len(codes),20):
        batch,meta=collect_focus_feedback(conn,cutoff,today,codes[offset:offset+20]);feedback.update(batch);feedback_meta.append(meta)
    scored=[]
    for p in source:
        if not p.get('industry_name') and p['stock_code'] in industries:
            p['industry_name']=industries[p['stock_code']]['industry_name']
        p['filings']=by_code.get(p['stock_code'],[])
        scored.append(score_candidate(p,run['as_of_date'],feedback.get(p['stock_code'])))
    return scored,previous,previous_run,run,{'snapshot_date':str(run['as_of_date']),'stale_sessions':stale,
        'source_stocks':len(source),'filings':len(filings),'filing_companies':len(by_code),'filings_truncated':truncated,
        'industry_policy':'证券主数据分类优先；缺失时使用两年内最近机构研报分类，未知行业共享限额',
        'feedback_batches':[{'status':m.get('status'),'coverage':m.get('coverage'),'error_type':m.get('error_type')} for m in feedback_meta]}

def update(conn,cutoff,cfg,apply=False):
    if apply and not conn.execute('SELECT pg_try_advisory_xact_lock(%s) ok',(LOCK,)).fetchone()['ok']:raise RuntimeError('Candidate update already running')
    scored,previous,prev_run,snapshot,collection=collect(conn,cutoff,cfg)
    today=cutoff.astimezone(TZ).date()
    # Do not include wall-clock polling time; same-day identical inputs reuse the run.
    stable={'version':VERSION,'date':str(today),'config':cfg,'source':str(snapshot['snapshot_id']),
            'scored':scored,'feedback_coverage':collection['feedback_batches']}
    identity=hashlib.sha256(json.dumps(stable,sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()
    existing=conn.execute('SELECT run_id,summary FROM analytics.candidate_pool_runs WHERE input_fingerprint=%s',(identity,)).fetchone()
    if existing:return {'status':'unchanged','run_id':str(existing['run_id']),'summary':existing['summary']}
    rows,summary=evolve(scored,previous,snapshot['as_of_date'],cutoff,cfg,prev_run)
    summary.update(collection=collection,config=cfg,score_policy='fundamentals_and_timing_separate; unsigned_filings_recall_only',
                   feedback_policy='mature_error_review_penalty_only; absence_is_not_negative',as_of_market_date=str(snapshot['as_of_date']))
    if apply:
        run=conn.execute('''INSERT INTO analytics.candidate_pool_runs(version,as_of_date,decision_cutoff,input_fingerprint,
            source_snapshot_id,weekly_key,monthly_key,summary) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING run_id''',
            (VERSION,snapshot['as_of_date'],cutoff,identity,snapshot['snapshot_id'],summary['weekly_key'],summary['monthly_key'],jsonb(summary))).fetchone()
        with conn.cursor() as cur:
            cur.executemany('INSERT INTO analytics.candidate_pool_revisions VALUES(%s,%s,%s,%s,%s,%s)',
                [(run['run_id'],p['stock_code'],p['state'],p['rank'],p['score'],jsonb(p)) for p in rows])
    return {'status':'updated' if apply else 'dry_run','summary':summary,'rows':len(rows),
        'preview':[{k:p.get(k) for k in ('stock_code','name','state','rank','long_term_score','timing_score')} for p in rows[:20]]}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');p.add_argument('--config',type=Path);p.add_argument('--output',type=Path)
    a=p.parse_args();cfg=config(a.config);cutoff=dt.datetime.now(TZ)
    with connect(autocommit=False,attempts=1) as c:
        c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'+('' if a.apply else ' READ ONLY'))
        c.execute("SET LOCAL statement_timeout='25s'");c.execute("SET LOCAL lock_timeout='3s'")
        result=update(c,cutoff,cfg,a.apply)
        if a.apply:c.commit()
        else:c.rollback()
    text=json.dumps(result,ensure_ascii=False,default=str,indent=2)
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(text+'\n')
    print(text)

if __name__=='__main__':main()
