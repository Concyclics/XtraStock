"""Build a frozen, append-only plan for the daily investment report.

Default execution prints a dry-run summary. --apply writes only the paper-plan
ledger. The controller prepares it before narrative generation; delivery stays gated.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import datetime as dt
import json
from pathlib import Path
try:
    from .common import connect,jsonb
    from .value_plan import VERSION,load_config,stamp,balanced_consensus,valuations,build_plans,digest,render
except ImportError:
    from common import connect,jsonb
    from value_plan import VERSION,load_config,stamp,balanced_consensus,valuations,build_plans,digest,render

EARNINGS_SQL='''WITH reports AS MATERIALIZED (
 SELECT * FROM research.reports_resolved WHERE report_type='stock'
 AND report_date BETWEEN %(since)s AND %(day)s
 AND greatest(coalesce(available_at,created_at),identity_known_at)<=%(cutoff)s
), dated AS (
 SELECT r.stock_code,r.institution,r.document_id,r.eastmoney_infocode source_identifier,r.report_date,
 f.forecast_year,'eps'::text metric,f.eps value,'CNY/share'::text unit,
 'explicit_dated_report_text'::text source_basis,
 greatest(r.available_at,r.identity_known_at,f.created_at,f.updated_at) known_at,
 greatest(f.created_at,f.updated_at) observed_at,
 jsonb_build_object('source_url',r.source_url,'evidence',f.metadata->'evidence',
   'content_sha256',f.metadata->'content_sha256') evidence
 FROM reports r JOIN research.report_forecasts f USING(document_id)
 WHERE f.eps IS NOT NULL AND f.forecast_year=%(fiscal_year)s
 AND f.metadata->>'semantic_contract'='earnings-forecast-v2'
 AND f.metadata->>'year_verified'='true' AND f.metadata->>'year_basis'='explicit_dated_report_text'
 AND f.metadata->>'eps_unit'='CNY/share'
 AND greatest(f.created_at,f.updated_at)<=%(cutoff)s
)
SELECT * FROM dated UNION ALL
SELECT o.stock_code,o.institution,o.document_id,o.source_identifier,o.report_date,o.forecast_year,
 o.metric,o.value,o.unit,o.source_basis,o.known_at,o.observed_at,o.evidence
FROM research.earnings_observations o WHERE o.report_date BETWEEN %(since)s AND %(day)s
 AND o.known_at<=%(cutoff)s AND o.forecast_year=%(fiscal_year)s AND o.metric='eps'
'''

def publication_approval(config,cutoff):
    if config['mode']!='approved' or not config.get('approval_file'):return False,{'passed':False,'reason':'shadow_validation_pending'}
    receipt=json.loads(Path(config['approval_file']).read_text())
    checks=receipt.get('checks',{})
    required={'net_return','drawdown','win_rate','complete_years','baseline_return','baseline_win',
        'historical_execution','point_in_time','calibration','forward_shadow'}
    bound={k:v for k,v in config.items() if k not in ('approval_file','mode','publish_enabled','enabled_from')}
    valid=(receipt.get('strategy_version')==VERSION and receipt.get('config_digest')==digest(bound)
        and set(checks)==required and all(v is True for v in checks.values())
        and receipt.get('evidence_digest') and receipt.get('valid_until')
        and stamp(receipt['valid_until'])>=stamp(cutoff))
    if not valid:raise ValueError('plan approval does not match this configuration or valid evidence')
    return True,receipt

def load_inputs(conn,cutoff,config):
    cutoff=stamp(cutoff);day=cutoff.date()
    latest=conn.execute('''SELECT * FROM analytics.candidate_pool_runs WHERE created_at<=%s AND decision_cutoff<=%s
        ORDER BY created_at DESC LIMIT 1''',(cutoff,cutoff)).fetchone()
    if not latest:raise ValueError('candidate pool missing before cutoff')
    market_date=latest['as_of_date']
    newer=conn.execute('''SELECT count(*) n FROM market.trading_calendar WHERE exchange='SSE' AND is_verified
        AND session_status='open' AND trade_date>%s AND close_at<=%s''',(market_date,cutoff)).fetchone()['n']
    if newer>1:raise ValueError('candidate snapshot more than one completed session behind')
    pool=[r['payload'] for r in conn.execute("SELECT payload FROM analytics.candidate_pool_revisions WHERE run_id=%s AND state IN('core','watching','exploring')",(latest['run_id'],))]
    params={'since':day-dt.timedelta(days=config['forecast_max_age_days']),'day':day,'cutoff':cutoff,'fiscal_year':day.year}
    obs=conn.execute(EARNINGS_SQL,params).fetchall()
    assets=conn.execute('''WITH industry AS MATERIALIZED (
      SELECT DISTINCT ON(stock_code) stock_code,industry_name FROM research.reports_resolved
      WHERE nullif(industry_name,'') IS NOT NULL AND report_date<=%s AND report_date>=%s
      AND greatest(coalesce(available_at,created_at),identity_known_at)<=%s
      ORDER BY stock_code,report_date DESC,document_id
    ) SELECT a.asset_id,a.symbol stock_code,a.name,coalesce(nullif(a.industry_name,''),i.industry_name) industry,
      b.close,b.dt market_date,b.quality_status,b.available_at
      FROM market.assets a LEFT JOIN market.canonical_bars_daily_v2 b ON b.asset_id=a.asset_id AND b.dt=%s
      LEFT JOIN industry i ON i.stock_code=a.symbol
      WHERE a.asset_type='stock' AND NOT(a.metadata ? 'identity_alias_of')''',
        (day,params['since'],cutoff,market_date)).fetchall()
    actions=conn.execute('''SELECT a.symbol stock_code,c.ex_date FROM market.corporate_actions c
       JOIN market.assets a USING(asset_id) WHERE c.ex_date BETWEEN %s AND %s
       AND c.quality_status<>'rejected' AND (coalesce(c.split_ratio,0)<>0 OR coalesce(c.stock_dividend_ratio,0)<>0
       OR coalesce(c.rights_ratio,0)<>0 OR c.action_type IN('split','reverse_split','rights_issue'))''',
       (params['since'],day)).fetchall()
    by_code=defaultdict(list);by_action=defaultdict(list)
    for r in obs:by_code[r['stock_code']].append(r)
    for r in actions:by_action[r['stock_code']].append({**r,'changes_share_basis':True})
    data=[]
    for a in assets:
        price=float(a['close']) if a['close'] and a['available_at'] and a['available_at']<=cutoff else None
        sector_rows=[r for r in by_code[a['stock_code']] if (r.get('evidence') or {}).get('industry')]
        sector=max(sector_rows,key=lambda r:(r['report_date'],r['known_at'])) if sector_rows else None
        industry=a['industry'] or (sector['evidence']['industry'] if sector else None)
        data.append({**a,'industry':industry,'close':price,'consensus':balanced_consensus(by_code[a['stock_code']],cutoff,day.year,
            config['forecast_max_age_days'],by_action[a['stock_code']])})
    values={v['stock_code']:v for v in valuations(data,config)}
    candidates=[]
    for p in pool:
        v=values.get(p['stock_code'])
        if not v:continue
        row={**v,'financial_eligible':bool(p.get('core_eligible')),'pool_score':float(p.get('score') or 0),'pool_state':p['state']}
        if v['quality_status']!='verified':row['valuation_gaps'].append('收盘价尚未通过多源核验')
        candidates.append(row)
    candidates.sort(key=lambda r:(bool(r['valuation_gaps']),not r['financial_eligible'],-r['pool_score'],r['stock_code']))
    chosen=[];sectors=defaultdict(int)
    for row in candidates:
        sector=row.get('industry') or 'unclassified'
        if sectors[sector]>=config['max_sector_candidates'] and sector!='unclassified':continue
        chosen.append(row);sectors[sector]+=1
        if len(chosen)>=config['max_candidates']:break
    sessions=[str(r['trade_date']) for r in conn.execute('''SELECT trade_date FROM market.trading_calendar
        WHERE exchange='SSE' AND is_verified AND session_status='open' AND trade_date BETWEEN %s AND %s ORDER BY trade_date''',
        (day,day+dt.timedelta(days=120)))]
    coverage={'pool_stocks':len(pool),'core_stocks':sum(p['state']=='core' for p in pool),
        'pool_positive_eps':sum(bool(values.get(p['stock_code'],{}).get('consensus',{}).get('eps_median',0) and values[p['stock_code']]['consensus']['eps_median']>0) for p in pool),
        'core_positive_eps':sum(p['state']=='core' and bool(values.get(p['stock_code'],{}).get('consensus',{}).get('eps_median',0) and values[p['stock_code']]['consensus']['eps_median']>0) for p in pool),
        'pool_minimum_brokers':sum(values.get(p['stock_code'],{}).get('consensus',{}).get('institutions',0)>=config['min_institutions'] for p in pool),
        'pool_valuation_ready':sum(not values[p['stock_code']]['valuation_gaps'] for p in pool if p['stock_code'] in values),
        'observations_read':len(obs),'all_price_eps_rows':sum(v['forward_pe'] is not None for v in values.values())}
    return chosen,sessions,market_date,coverage

def run(conn,report_date,cutoff,config,apply=False,require_report=True):
    existing=conn.execute('SELECT * FROM quant.daily_plan_runs WHERE strategy_version=%s AND report_date=%s',
        (VERSION,report_date)).fetchone()
    if existing:return {'status':'already_frozen','run_id':existing['run_id'],'summary':existing['summary']}
    approved,validation=publication_approval(config,cutoff)
    report=conn.execute('''SELECT report_run_id,completed_at,status FROM agent.report_runs
      WHERE report_date=%s AND report_type='ai_hardware_daily_v3' AND status IN('published','degraded')
      AND completed_at<=%s ORDER BY completed_at DESC LIMIT 1''',(report_date,cutoff)).fetchone() if require_report else None
    if require_report and not report:raise ValueError('no successfully published report before plan cutoff')
    if approved and report and report['status']!='published':raise ValueError('recommendations require a fully validated report')
    rows,sessions,market_date,coverage=load_inputs(conn,cutoff,config)
    prev=conn.execute('''SELECT p.payload FROM quant.daily_plan_revisions p JOIN quant.daily_plan_runs r USING(run_id)
       WHERE r.strategy_version=%s AND r.report_date=(SELECT max(report_date) FROM quant.daily_plan_runs
       WHERE strategy_version=%s AND report_date<%s)''',(VERSION,VERSION,report_date)).fetchall()
    # This is the earliest internal availability, never an assertion of QQ delivery.
    # The delivery receipt supplies the actual publication time for paper orders.
    positions=conn.execute("SELECT state FROM quant.daily_plan_accounts WHERE account_id='daily-value-plan-shadow-v1'").fetchone()
    quantities=defaultdict(int)
    for lot in (positions['state'].get('lots',[]) if positions else []):quantities[lot['code']]+=int(lot['qty'])
    predecessors=[]
    for record in prev:
        item={**record['payload'],'paper_quantity':quantities[record['payload']['stock_code']]}
        if item['paper_quantity']:item['state']='held'
        predecessors.append(item)
    plans=build_plans(rows,predecessors,cutoff,cutoff,sessions,config,approved)
    for p in plans:p['publication_status']='not_yet_delivered'
    source_digest=digest(rows);run_id=digest([VERSION,str(report_date),source_digest,str(cutoff),digest(config)])
    summary={**coverage,'plans':len(plans),'changes':sum(p['change']!='retained' for p in plans),
        'actionable_buys':sum(p['state']=='eligible_to_buy' for p in plans),'approved':approved,
        'report_status':report['status'] if report else 'prepared_for_report' if apply else 'offline_test'}
    if apply:
        conn.execute('''INSERT INTO quant.daily_plan_runs(run_id,strategy_version,report_date,decision_at,evidence_cutoff,
          market_date,report_run_id,mode,config_digest,source_digest,validation,summary)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
          (run_id,VERSION,report_date,cutoff,cutoff,market_date,report['report_run_id'] if report else None,
           'approved' if approved else 'shadow',digest(config),source_digest,jsonb(validation),jsonb(summary)))
        for p in plans:
            conn.execute('''INSERT INTO quant.daily_plan_revisions(plan_id,run_id,stock_code,previous_plan_id,state,payload)
               VALUES(%s,%s,%s,%s,%s,%s)''',(p['plan_id'],run_id,p['stock_code'],p.get('previous_plan_id'),p['state'],jsonb(json.loads(json.dumps(p,default=str)))))
    return {'status':'frozen' if apply else 'dry_run','run_id':run_id,'summary':summary,'plans':plans,
        'preview':render(plans,report_date,approved),'validation':validation}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--date');p.add_argument('--config',type=Path)
    p.add_argument('--apply',action='store_true');p.add_argument('--output',type=Path)
    p.add_argument('--without-report',action='store_true',help='Offline acceptance only; cannot write a run.')
    p.add_argument('--prepare-for-report',action='store_true',help='Freeze the plan before report generation; publication is recorded only after the complete report is delivered.')
    a=p.parse_args();cutoff=stamp(dt.datetime.now(dt.timezone.utc));day=dt.date.fromisoformat(a.date) if a.date else cutoff.date()
    if day!=cutoff.date() and a.apply:raise SystemExit('Cannot backdate live plan generation')
    if a.apply and a.without_report:raise SystemExit('Use --prepare-for-report to freeze a live plan before the report')
    if a.prepare_for_report and a.without_report:raise SystemExit('Choose either live preparation or offline acceptance')
    with connect(autocommit=False) as conn:
        conn.execute("SET LOCAL statement_timeout='120s'");conn.execute("SET LOCAL lock_timeout='3s'")
        if a.apply:conn.execute('SELECT pg_advisory_xact_lock(704201611)')
        else:conn.execute('SET TRANSACTION READ ONLY')
        result=run(conn,day,cutoff,load_config(a.config),a.apply,not (a.without_report or a.prepare_for_report))
    if a.output:a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k in ('status','run_id','summary','validation')},ensure_ascii=False,default=str))
    return 0

if __name__=='__main__':raise SystemExit(main())
