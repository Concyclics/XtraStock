#!/usr/bin/env python3
"""Gate weekly research before mining; blocked inputs never launch expensive work."""
from __future__ import annotations

import sys
import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

from common import connect, repo_root
from factor_validation import purged_folds
from factor_input_contract import require_stock_horizon
from research_history import history_plan

EXPECTED_SQL="""/* weekly:expected */ SELECT max(trade_date) AS dt FROM market.trading_calendar
 WHERE exchange='SSE' AND is_verified AND session_status='open'
   AND trade_date BETWEEN %(as_of)s::date-30 AND %(as_of)s"""

DAILY_SQL="""/* weekly:daily */
WITH scored AS (
 SELECT max(dt) AS scored_dt,count(DISTINCT asset_id) AS scored_assets
 FROM factor.valid_daily_scores_v1 WHERE dt=%(expected_dt)s AND percentile IS NOT NULL
), last_run AS (
 SELECT status,end_date FROM ops.crawler_runs WHERE task_name='daily_factor_materialize'
 ORDER BY started_at DESC LIMIT 1
)
SELECT %(expected_dt)s::date AS expected_dt,s.scored_dt,s.scored_assets,
 l.status AS run_status,l.end_date AS run_end_date
FROM scored s LEFT JOIN last_run l ON true
"""

STRICT_FEATURES="""
SELECT f.asset_id,f.dt,f.decision_cutoff FROM factor.feature_daily f
JOIN market.assets a USING(asset_id)
WHERE f.dt BETWEEN %(since)s AND %(as_of)s
 AND f.input_contract_version='pit-input-v1' AND f.knowledge_mode='system'
 AND f.input_quality->>'strict_pit_ready'='true'
 AND f.available_at<=f.decision_cutoff AND f.decision_cutoff<=statement_timestamp()
 AND f.decision_cutoff<((f.dt+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
 AND a.asset_type='stock' AND NOT coalesce(a.metadata ? 'identity_alias_of',false)
"""


def collect_gate(conn,as_of,*,horizon=20,folds=3,test_days=80,train_years=5,ignore_daily_gate=False,min_train_sessions=500):
    require_stock_horizon(horizon)
    plan=history_plan(min_train_sessions,test_days,folds,horizon)
    test_sessions=plan['test_sessions'];needed=plan['required_eligible_sessions']
    params={'as_of':as_of,'since':as_of-dt.timedelta(days=366*train_years),'needed':needed}
    report={'status':'blocked','input_contract_version':'pit-input-v1','as_of':as_of,
            'horizon':horizon,'required_eligible_sessions':needed,'history_start':params['since'],
            'minimum_assets_per_session':30,'ignore_crawler_status':ignore_daily_gate,
            'reasons':[],'launched_commands':0,'history_plan':plan}
    expected=conn.execute(EXPECTED_SQL,params).fetchone()
    if not expected or not expected['dt']:
        report['reasons'].append('verified_current_calendar_missing');return report,[]
    # Bind the actual date as a parameter: putting a join-derived date around a
    # grouped view can prevent pushdown and hash all historical cohorts.
    params['expected_dt']=expected['dt']
    daily=conn.execute(DAILY_SQL,params).fetchone();report['daily']=daily
    # This cannot be bypassed by --ignore-daily-gate. Legacy raw scores, NULL
    # ranks and stale feature/cohort versions do not prove current readiness.
    if not daily or not daily['expected_dt'] or daily['scored_dt']!=daily['expected_dt'] or daily['scored_assets']<30:
        report['reasons'].append('current_strict_scores_not_ready');return report,[]
    if not ignore_daily_gate and (daily['run_status']!='ok' or daily['run_end_date'] is None or daily['run_end_date']<daily['expected_dt']):
        report['reasons'].append('daily_materialization_not_complete');return report,[]
    preflight=conn.execute("""/* weekly:strict_dates */ WITH f AS ("""+STRICT_FEATURES+""")
      SELECT dt FROM f GROUP BY dt HAVING count(*)>=30 ORDER BY dt DESC LIMIT %(needed)s""",params).fetchall()
    report['strict_feature_sessions_capped']=len(preflight)
    if len(preflight)<needed:
        report['reasons'].append('insufficient_strict_feature_history');return report,[]
    # Only after the small current-day gate and date-count preflight do we join
    # mature labels/lifecycle. No prices, training matrices or mining are read.
    label_sql="""/* weekly:mature_dates */ WITH f AS MATERIALIZED ("""+STRICT_FEATURES+f""")
      SELECT f.dt FROM f JOIN factor.forward_returns r ON r.asset_id=f.asset_id AND r.signal_date=f.dt
      JOIN LATERAL (
        SELECT v.* FROM market.asset_lifecycle_versions v WHERE v.asset_id=f.asset_id
          AND v.valid_from<=f.dt AND (v.valid_to IS NULL OR v.valid_to>=f.dt)
          AND v.available_at<=f.decision_cutoff
        ORDER BY v.available_at DESC,v.valid_from DESC,v.lifecycle_version_id DESC LIMIT 1
      ) life ON life.lifecycle_status='active' AND life.is_tradable
        AND NOT life.is_st AND NOT life.is_suspended AND life.quality_status='verified'
      WHERE r.input_contract_version='pit-input-v1' AND r.return_{horizon}d IS NOT NULL
        AND r.label_available_at_{horizon}d<=statement_timestamp()
        AND r.exit_date_{horizon}d<=%(as_of)s
      GROUP BY f.dt HAVING count(*)>=30 ORDER BY f.dt
    """
    dates=[r['dt'] for r in conn.execute(label_sql,params).fetchall()]
    report['mature_strict_eligible_sessions']=len(dates)
    windows=purged_folds(dates,folds=folds,test_sessions=test_sessions,gap=horizon,min_train_sessions=min_train_sessions)
    report['available_independent_folds']=len(windows)
    if len(windows)<folds:
        report['reasons'].append('insufficient_mature_strict_label_history');return report,[]
    report['status']='ready';report['folds']=windows
    return report,windows


def run(command):
    print('+ '+' '.join(command),flush=True)
    subprocess.run(command,cwd=repo_root(),check=True)


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--mode',choices=('strict','reconstruction','configured'),default='strict')
    p.add_argument('--config',type=Path)
    p.add_argument('--min-train-sessions',type=int)
    p.add_argument('--test-days',type=int)
    p.add_argument('--folds',type=int)
    p.add_argument('--train-years',type=int,default=5,help='Strict lane history lookback in years.')
    p.add_argument('--history-days',type=int,help='Reconstruction lane history lookback in calendar days.')
    p.add_argument('--horizon',type=int,choices=(5,20,60))
    p.add_argument('--max-candidates',type=int)
    p.add_argument('--ignore-daily-gate',action='store_true',help='Ignore crawler status only; strict input and history gates always apply.')
    p.add_argument('--dry-run',action='store_true',help='Read-only gate inspection; never launch mining or evaluation.')
    p.add_argument('--report',type=Path,help='Save the small gate result as local JSON.')
    args=p.parse_args(argv)
    if args.mode!='strict':
        from candidate_pool import config
        cfg=config(args.config)
        if args.mode=='configured':args.mode=cfg['mining']['mode']
        if args.mode not in ('strict','reconstruction'):p.error('invalid configured mining mode')
        for arg,key in [('min_train_sessions','min_train_sessions'),('test_days','test_sessions'),('folds','folds')]:
            if getattr(args,arg) is None:setattr(args,arg,cfg['mining'][key])
        if args.mode=='reconstruction':
            for arg,key in [('min_train_sessions','min_train_sessions'),('test_days','test_sessions'),('folds','folds'),
                            ('history_days','history_days'),('max_candidates','max_candidates')]:
                if getattr(args,arg) is not None:cfg['mining'][key]=getattr(args,arg)
            if args.horizon is not None:cfg['mining']['horizons']=[args.horizon]
            from reconstructed_factor_research import run_reconstruction
            try:report=run_reconstruction(cfg,args.dry_run)
            except Exception as exc:
                report={'status':'failed','mode':'public_reconstruction','error_type':type(exc).__name__,
                    'phase':getattr(exc,'phase','reconstruction'),'input_read':getattr(exc,'read_metrics',{}),
                    'production_approved':False}
            if args.report:
                args.report.parent.mkdir(parents=True,exist_ok=True)
                args.report.write_text(json.dumps(report,ensure_ascii=False,default=str,indent=2)+'\n')
            print(json.dumps(report,ensure_ascii=False,default=str),flush=True)
            return 0 if report.get('status') in ('complete','planned','partial') else 1
    args.min_train_sessions=args.min_train_sessions if args.min_train_sessions is not None else 500
    args.horizon=args.horizon if args.horizon is not None else 20
    args.max_candidates=args.max_candidates if args.max_candidates is not None else 24
    if args.history_days is not None:p.error('--history-days applies only to reconstruction mode; use --train-years for strict mode')
    args.test_days=args.test_days if args.test_days is not None else 80
    args.folds=args.folds if args.folds is not None else 3
    try:history_plan(args.min_train_sessions,args.test_days,args.folds,args.horizon)
    except ValueError as exc:p.error(str(exc))
    if min(args.folds,args.train_years,args.test_days)<=0:p.error('folds, train-years and test-days must be positive')
    today=dt.datetime.now(ZoneInfo('Asia/Shanghai')).date()
    try:
        with connect(autocommit=False) as conn:
            conn.execute('SET TRANSACTION READ ONLY');conn.execute("SET LOCAL statement_timeout='25s'")
            conn.execute("SET LOCAL lock_timeout='2s'")
            report,windows=collect_gate(conn,today,horizon=args.horizon,folds=args.folds,test_days=args.test_days,
                train_years=args.train_years,ignore_daily_gate=args.ignore_daily_gate,min_train_sessions=args.min_train_sessions)
            conn.rollback()
    except Exception as exc:
        # Driver diagnostics may contain connection details: retain the type and
        # SQLSTATE, not a connection string or arbitrary exception payload.
        report={'status':'blocked','reasons':['input_readiness_check_failed'],
                'error_type':type(exc).__name__,'sqlstate':getattr(exc,'sqlstate',None),
                'launched_commands':0,'as_of':today};windows=[]
    report['dry_run']=args.dry_run
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,default=str,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False,default=str),flush=True)
    if args.dry_run or report['status']!='ready':return 0
    python=sys.executable
    run([python,'scripts/analytics/mine_factor_candidates.py','--max-candidates',str(args.max_candidates)])
    for train_start,train_end,test_start,test_end in windows:
        run([python,'scripts/analytics/evaluate_factors.py','--train-start',str(train_start),'--train-end',str(train_end),
             '--test-start',str(test_start),'--test-end',str(test_end),'--horizon',str(args.horizon),
             '--status','candidate,shadow','--promote-qualified'])
    run([python,'scripts/analytics/build_factor_scores.py','--since',str(test_end-dt.timedelta(days=45)),'--until',str(test_end)])
    print(json.dumps({'status':'complete','horizon':args.horizon,'independent_folds':len(windows),'production_promotion':False}))
    return 0

if __name__=='__main__':raise SystemExit(main())
