#!/usr/bin/env python3
"""Update bounded research learning, then attention, with immutable selection evidence."""
from __future__ import annotations
import argparse
import datetime as dt
import json
from pathlib import Path
import re
import sys
import time
import uuid

from common import ROOT, today, pipeline_lock, run_logged, log_path, connect, TZ
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.analytics.forecast_learning import DEFAULT_CODES

SELECTION_VERSION='research-learning-candidates:v1'
MAX_FOCUS_ADDITIONS=11
MAX_FOCUS_ROWS=64
FOCUS_RUN_SQL="""SELECT run_id,as_of_date,decision_cutoff,created_at
 FROM analytics.research_focus_runs
 WHERE created_at<=%s AND decision_cutoff<=%s AND as_of_date<=%s
 ORDER BY created_at DESC,run_id DESC LIMIT 1"""
FOCUS_ROWS_SQL="""SELECT revision_id,focus_key,entity_type,label,state,priority,stock_codes,created_at,
 count(*) OVER() AS visible_rows_total
 FROM analytics.research_focus_revisions
 WHERE run_id=%s AND created_at<=%s AND entity_type='stock' AND state IN ('active','watching')
 ORDER BY CASE state WHEN 'active' THEN 0 ELSE 1 END,priority DESC,focus_key LIMIT %s"""


def _timestamp(value):
    at=value if isinstance(value,dt.datetime) else dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if at.tzinfo is None:raise ValueError('selection evidence timestamps must include timezone')
    return at


def base_selection(cutoff):
    if len(DEFAULT_CODES)!=9 or len(set(DEFAULT_CODES))!=9:
        raise ValueError('the reviewed reference universe must contain exactly nine distinct securities')
    return {'version':SELECTION_VERSION,'selection_cutoff':cutoff.isoformat(),
      'focus_date_ceiling':cutoff.astimezone(TZ).date().isoformat(),'status':'complete',
      'reference_codes':list(DEFAULT_CODES),'focus_codes':[],'stock_codes':list(DEFAULT_CODES),
      'selected_count':9,'focus_additions_limit':MAX_FOCUS_ADDITIONS,'total_limit':20,
      'source_focus_run':None,'focus_rows_examined':0,'focus_rows_available':None,
      'source_query_limit':MAX_FOCUS_ROWS,'source_truncated':False,'errors':[],
      'selections':[{'stock_code':code,'reason':'retained_fixed_reference_research_asset','source':'forecast_learning.DEFAULT_CODES'} for code in DEFAULT_CODES],
      'limitations':['Research sample selection, not a buy/sell recommendation or model approval.',
       'Focus-selected and fixed reference assets have selection/survivorship bias; not a market-wide return estimate.',
       'Selection is recorded at actual execution time, never backdated to the learning price date.',
       'No theme constituents are expanded; missing history remains the learning module\'s explicit data limitation.']}


def build_candidate_selection(run,rows,cutoff):
    if run and run.get('source')=='candidate_pool':
        as_of=dt.date.fromisoformat(str(run['as_of_date']))
        if _timestamp(run['created_at'])>cutoff or _timestamp(run['decision_cutoff'])>cutoff or as_of>cutoff.astimezone(TZ).date():
            raise ValueError('candidate pool not visible at cutoff')
        eligible=list(dict.fromkeys(r['stock_code'] for r in sorted(rows,key=lambda r:r['rank'])
            if re.fullmatch(r'\d{6}',r['stock_code'])))
        codes=eligible[:20]
        return {'version':'research-learning-candidates:v2','selection_cutoff':cutoff.isoformat(),
            'status':'complete' if codes else 'unknown','reference_codes':[],'focus_codes':codes,'stock_codes':codes,
            'selected_count':len(codes),'total_limit':20,'source_candidate_run':str(run['run_id']),
            'selection_reason':'core then weekly watch rank, then daily exploration; fixed references no longer occupy slots',
            'source_truncated':len(eligible)>20,'errors':[] if codes else [{'reason':'no_eligible_pool_company'}],
            'limitations':['Current candidate selection has survivorship/selection bias in historical reconstruction.']}
    result=base_selection(cutoff)
    if not run:
        result.update(status='unknown',errors=[{'reason':'no_focus_run_visible_at_selection_cutoff'}])
        return result
    ceiling=cutoff.astimezone(TZ).date()
    as_of=run['as_of_date'] if isinstance(run['as_of_date'],dt.date) else dt.date.fromisoformat(str(run['as_of_date']))
    if _timestamp(run['created_at'])>cutoff or _timestamp(run['decision_cutoff'])>cutoff or as_of>ceiling:
        raise ValueError('focus run is later than actual selection cutoff')
    result['source_focus_run']={k:str(run[k]) for k in ('run_id','as_of_date','decision_cutoff','created_at')}
    # Enforce the ranking contract even with a substituted reader/test fixture.
    rows=sorted(rows,key=lambda r:(0 if r['state']=='active' else 1,-float(r['priority']),r['focus_key']))[:MAX_FOCUS_ROWS]
    result['focus_rows_examined']=len(rows)
    result['focus_rows_available']=max((int(r.get('visible_rows_total',len(rows))) for r in rows),default=0)
    result['source_truncated']=result['focus_rows_available']>len(rows)
    seen=set(DEFAULT_CODES);excluded=[];eligible=0
    for row in rows:
        code=str(row.get('focus_key','')).removeprefix('stock:')
        if row.get('entity_type')!='stock' or row.get('state') not in ('active','watching'):
            excluded.append({'focus_key':row.get('focus_key'),'reason':'not_direct_active_or_watching_company'});continue
        if _timestamp(row['created_at'])>cutoff:
            excluded.append({'focus_key':row.get('focus_key'),'reason':'revision_not_visible_at_cutoff'});continue
        if not re.fullmatch(r'\d{6}',code) or row.get('focus_key')!='stock:'+code or row.get('stock_codes')!=[code]:
            result['errors'].append({'focus_key':row.get('focus_key'),'reason':'ambiguous_direct_stock_identity'})
            continue
        if code in seen:continue
        eligible+=1
        if len(result['focus_codes'])>=MAX_FOCUS_ADDITIONS:continue
        seen.add(code);result['focus_codes'].append(code);result['stock_codes'].append(code)
        result['selections'].append({'stock_code':code,'reason':'latest_visible_focus_direct_company; active before watching, then descending research priority',
          'source':'analytics.research_focus_revisions','focus_revision_id':str(row['revision_id']),
          'focus_run_id':str(run['run_id']),'focus_as_of_date':str(as_of),'focus_state':row['state'],
          'focus_priority':float(row['priority']),'focus_key':row['focus_key']})
    result.update(selected_count=len(result['stock_codes']),eligible_nonreference_focus_rows=eligible,
                  nonselected_focus_rows=max(0,eligible-len(result['focus_codes'])),excluded_rows=excluded)
    if result['errors']:result['status']='partial'
    return result


def read_focus_candidates(cutoff):
    with connect() as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            conn.execute("SET LOCAL statement_timeout='25s'")
            if conn.execute("SELECT to_regclass('analytics.candidate_pool_runs') present").fetchone()['present']:
                run=conn.execute('''SELECT *, 'candidate_pool' source FROM analytics.candidate_pool_runs
                    WHERE created_at<=%s AND decision_cutoff<=%s AND as_of_date<=%s
                    ORDER BY created_at DESC LIMIT 1''',(cutoff,cutoff,cutoff.astimezone(TZ).date())).fetchone()
                if run:
                    from scripts.analytics.candidate_pool import config
                    rows=conn.execute('''SELECT stock_code,rank FROM analytics.candidate_pool_revisions
                        WHERE run_id=%s AND state IN ('core','watching','exploring') ORDER BY rank LIMIT %s''',
                        (run['run_id'],config()['learning_stock_limit'])).fetchall()
                    return run,rows
            run=conn.execute(FOCUS_RUN_SQL,(cutoff,cutoff,cutoff.astimezone(TZ).date())).fetchone()
            rows=conn.execute(FOCUS_ROWS_SQL,(run['run_id'],cutoff,MAX_FOCUS_ROWS)).fetchall() if run else []
    return run,rows


def select_learning_candidates(cutoff):
    try:
        run,rows=read_focus_candidates(cutoff)
        return build_candidate_selection(run,rows,cutoff)
    except Exception as exc:
        result=base_selection(cutoff)
        result.update(status='error',errors=[{'reason':'focus_selection_read_failed','error_type':type(exc).__name__}])
        return result


def completed_learning_date(as_of,cutoff):
    with connect() as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SET LOCAL statement_timeout='25s'")
            day=conn.execute("""SELECT max(trade_date) d FROM market.trading_calendar
                WHERE exchange='SSE' AND session_status='open' AND is_verified
                AND trade_date<=%s AND close_at<=%s AND available_at<=%s""",(as_of,cutoff,cutoff)).fetchone()['d']
    if day is None:raise ValueError('no completed verified session for learning')
    return day


def stage_commands(as_of,scope='all',learning_date=None,stock_codes=None,focus_date=None):
    stages=[]
    if scope in ('all','learning'):
        command=[str(ROOT/sys.executable),'scripts/analytics/run_forecast_learning_cycle.py',
                 '--as-of',(learning_date or as_of).isoformat()]
        if stock_codes is not None:command.extend(['--stock-codes',','.join(stock_codes)])
        stages.append(('learning',command))
    if scope in ('all','focus'):
        stages.append(('focus',[str(ROOT/sys.executable),'scripts/analytics/update_research_focus.py',
                               '--apply','--as-of',(focus_date or as_of).isoformat()]))
    return stages


def prepare(as_of,scope,cutoff):
    """Preflight failure is represented as a learning stage failure, not a focus abort."""
    state={'requested_date':as_of.isoformat(),'execution_cutoff':cutoff.isoformat(),
           'learning_date':None,'focus_refresh_date':cutoff.astimezone(TZ).date(),
           'selection':None,'preflight_errors':[]}
    if scope in ('all','learning'):
        try:state['learning_date']=completed_learning_date(as_of,cutoff)
        except Exception as exc:
            state['preflight_errors'].append({'component':'learning_calendar','error_type':type(exc).__name__})
        state['selection']=select_learning_candidates(cutoff)
    return state


def execute_stages(as_of,scope,prepared):
    selection=prepared['selection'];codes=selection['stock_codes'] if selection else None
    stages=stage_commands(as_of,scope,prepared['learning_date'],codes,prepared['focus_refresh_date'])
    code=int(bool(prepared['preflight_errors']) or bool(selection and selection['status']!='complete'))
    outcomes=[]
    for name,command in stages:
        if name=='learning' and prepared['learning_date'] is None:
            outcomes.append({'stage':'learning','exit_code':1,'status':'preflight_failed',
              'errors':prepared['preflight_errors'],'command_started':False,'seconds':0})
            continue
        log=log_path('research_intelligence_'+name);t=time.monotonic()
        try:
            run_logged(command,log)
            item={'stage':name,'exit_code':0,'log':str(log),'command_started':True}
        except Exception as exc:
            code=1
            item={'stage':name,'exit_code':getattr(exc,'returncode',1),'log':str(log),
                  'error_type':type(exc).__name__,'command_started':True}
        item['seconds']=round(time.monotonic()-t,3);outcomes.append(item)
    return outcomes,code


def write_immutable(path,value):
    with path.open('x') as handle:
        handle.write(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date')
    parser.add_argument('--scope',choices=('all','focus','learning'),default='all')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    as_of=dt.date.fromisoformat(args.date) if args.date else today()
    started=dt.datetime.now(dt.timezone.utc)
    if as_of>started.astimezone(TZ).date():parser.error('date cannot be in the future')
    if args.dry_run:
        prepared=prepare(as_of,args.scope,started)
        selection=prepared['selection']
        print(json.dumps({**prepared,'stages':stage_commands(as_of,args.scope,prepared['learning_date'],
              selection['stock_codes'] if selection else None,prepared['focus_refresh_date']),'dry_run':True,'commands_started':0},default=str))
        return int(bool(prepared['preflight_errors']) or bool(selection and selection['status']!='complete'))
    output=ROOT/'logs/research_intelligence';output.mkdir(parents=True,exist_ok=True)
    run_key=started.strftime('%Y%m%dT%H%M%S%fZ')+'-'+uuid.uuid4().hex[:8]
    report={'as_of':as_of.isoformat(),'scope':args.scope,'started_at':started.isoformat(),'stages':[],
      'usage':'Research-only sample selection; no messages, orders, promotion, or historical selection backdating.'}
    with pipeline_lock('research_intelligence'):
        discovery=None
        if args.scope in ('all','focus'):
            pool_log=log_path('candidate_pool');begin=time.monotonic()
            run_logged([str(ROOT/sys.executable),'scripts/analytics/update_candidate_pool.py','--apply'],pool_log)
            discovery={'stage':'candidate_pool','exit_code':0,'log':str(pool_log),'seconds':round(time.monotonic()-begin,3)}
        # Selection must see the discovery just committed, never reuse the older
        # process start cutoff to conceal new candidates from this learning run.
        prepared=prepare(as_of,args.scope,dt.datetime.now(dt.timezone.utc))
        selection_path=output/(run_key+'.selection.json')
        # Persist the exact inputs before launching learning, including degraded fallbacks.
        write_immutable(selection_path,prepared)
        report.update(preflight=prepared,selection_report=str(selection_path))
        report['stages'],code=execute_stages(as_of,args.scope,prepared)
        if discovery:report['stages'].insert(0,discovery)
        report.update(completed_at=dt.datetime.now(dt.timezone.utc).isoformat(),exit_code=code)
        write_immutable(output/(run_key+'.json'),report)
        temp=output/'latest.json.tmp';temp.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n');temp.replace(output/'latest.json')
    print(json.dumps(report,ensure_ascii=False,default=str));return code


if __name__=='__main__':sys.exit(main())
