"""Bounded, incremental research refresh and prospective outcome ledger.

Example: .venv/bin/python scripts/analytics/run_forecast_learning_cycle.py --as-of 2026-10-13
No model is promoted; no orders or messages are sent. Unchanged mature inputs reuse
an immutable model. New/revised inputs trigger a CPU-only bounded batch refit.
"""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()

import argparse
import datetime as dt
import gzip
import json
import os
from pathlib import Path
import sys
import time
import uuid

# Bound tiny linear algebra jobs even when called outside the pipeline.
os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
os.environ.setdefault('OMP_NUM_THREADS','1')
from common import connect, jsonb, repo_root
from forecast_learning import (VERSION,CONTRACT,DEFAULT_CODES,LIMITATIONS,canonical_json,digest,
                               build_examples,train_research,predict,prediction_intervals,settle_prediction,can_record_forward)

LOCK_KEY=706131041
PRICE_SQL="""
WITH selected_assets AS MATERIALIZED (
 SELECT asset_id,symbol,name FROM market.assets
 WHERE asset_type='stock' AND NOT(metadata ? 'identity_alias_of') AND symbol=ANY(%s)
), prices AS MATERIALIZED (
 SELECT b.* FROM market.canonical_bars_daily_v2 b JOIN selected_assets a USING(asset_id)
 WHERE b.dt BETWEEN %s AND %s
), adjustments AS MATERIALIZED (
 SELECT f.* FROM market.adjustment_factors_daily f JOIN selected_assets a USING(asset_id)
 WHERE f.dt BETWEEN (SELECT min(dt) FROM prices) AND (SELECT max(dt) FROM prices)
)
SELECT a.symbol AS stock_code,a.name,b.dt,b.open*f.total_return_factor AS open,
       b.close*f.total_return_factor AS close,b.close AS raw_close,b.volume,b.quality_status,
       f.quality_status AS adjustment_quality,
       greatest(b.available_at,f.available_at,b.updated_at,f.updated_at) AS source_available_at,
       b.source,b.source_version,f.source_version AS adjustment_source_version,
       f.total_return_factor
FROM selected_assets a
JOIN prices b ON b.asset_id=a.asset_id
JOIN adjustments f ON f.asset_id=b.asset_id AND f.dt=b.dt
ORDER BY a.symbol,b.dt
"""
CALENDAR_SQL="""
SELECT trade_date,open_at,close_at,is_verified,source_name,available_at FROM market.trading_calendar
WHERE exchange='SSE' AND session_status='open'
 AND (is_verified OR source_name='observed market bars fallback')
 AND trade_date BETWEEN %s AND %s AND available_at<=%s ORDER BY trade_date
"""
PREDICTION_INSERT_SQL="""WITH recording AS MATERIALIZED (SELECT clock_timestamp() AS at)
    INSERT INTO analytics.learning_predictions_v1
    SELECT %s,%s,%s,%s,%s,%s,%s,%s,recording.at,'system_forward',%s,%s,%s,%s,%s
    FROM recording WHERE recording.at<%s::timestamptz
    ON CONFLICT(model_id,stock_code,signal_date,horizon) DO NOTHING RETURNING prediction_id"""


class LearningInputReadError(RuntimeError):
    def __init__(self,phase,metrics,cause):
        self.phase=phase
        self.read_metrics=metrics
        self.original_error_type=type(cause).__name__
        super().__init__(f'learning input read failed: {phase} ({self.original_error_type})')


def read_inputs(conn,codes,since,as_of,cutoff,*,metrics=None,budget_seconds=120):
    """One snapshot, serial batches; a failed batch never returns partial inputs."""
    codes=tuple(dict.fromkeys(codes))
    if not 1<=len(codes)<=40:raise ValueError('input read universe must contain 1..40 distinct securities')
    if not 0<budget_seconds<=120:raise ValueError('input read budget must be 0..120 seconds')
    started=time.monotonic();deadline=started+budget_seconds
    metrics=metrics if metrics is not None else {}
    metrics.update(status='running',securities=len(codes),batch_size=5,budget_seconds=budget_seconds,
                   maximum_statement_timeout_ms=25000,phases=[])
    phase='transaction_setup'
    def query(sql,params,name):
        nonlocal phase
        phase=name;begin=time.monotonic()
        item={'phase':name};metrics['phases'].append(item)
        try:
            remaining=deadline-begin
            if remaining<=0:raise TimeoutError('total input read budget exhausted')
            # Never increase the existing 25-second SQL limit. The final batch
            # receives only the remaining overall budget.
            timeout_ms=min(25000,max(1,int(remaining*1000)))
            conn.execute("SELECT set_config('statement_timeout',%s,true)",(str(timeout_ms),))
            rows=conn.execute(sql,params).fetchall()
            if time.monotonic()>deadline:raise TimeoutError('total input read budget exhausted')
            item.update(rows=len(rows),statement_timeout_ms=timeout_ms)
            return rows
        except Exception as exc:
            item['error_type']=type(exc).__name__
            raise
        finally:item['seconds']=round(time.monotonic()-begin,3)
    try:
        with conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            conn.execute("SET LOCAL lock_timeout='1s'")
            calendar=query(CALENDAR_SQL,(since,as_of+dt.timedelta(days=180),cutoff),'calendar')
            prices=[]
            for offset in range(0,len(codes),5):
                batch=codes[offset:offset+5]
                rows=query(PRICE_SQL,(list(batch),since,as_of),f'price_batch_{offset//5+1}')
                metrics['phases'][-1]['securities']=len(batch)
                prices.extend(rows)
            phase='merge_inputs'
            prices.sort(key=lambda row:(row['stock_code'],row['dt']))
            if time.monotonic()>deadline:raise TimeoutError('total input read budget exhausted')
        metrics.update(status='complete',price_rows=len(prices),calendar_rows=len(calendar),
                       seconds=round(time.monotonic()-started,3))
        return prices,calendar
    except Exception as exc:
        metrics.update(status='failed',failed_phase=phase,seconds=round(time.monotonic()-started,3),
                       partial_inputs_returned=False)
        raise LearningInputReadError(phase,metrics,exc) from exc


def read_cycle_inputs(conn,codes,pending,since,as_of,cutoff,metrics):
    """Keep old-pool outcome settlement working as the watched universe rotates.

    Training uses one snapshot. Additional outcome-only batches read just their
    holding periods, with the same knowledge cutoff and a shared 120s budget.
    No outcome or model is written unless all required reads succeed.
    """
    all_codes=tuple(dict.fromkeys((*codes,*(p['stock_code'] for p in pending))))
    if len(all_codes)<=40:
        return read_inputs(conn,all_codes,since,as_of,cutoff,metrics=metrics)
    started=time.monotonic();prices=[];calendar_by_day={}
    metrics.update(status='running',securities=len(all_codes),budget_seconds=120,batches=[],
        snapshot_policy='training_snapshot_and_separate_outcome_snapshots_at_one_knowledge_cutoff')
    extra=[code for code in all_codes if code not in codes]
    batches=[(list(codes),since)]
    for offset in range(0,len(extra),40):
        batch=extra[offset:offset+40]
        first=min(dt.date.fromisoformat(str(p['entry_date'])) for p in pending if p['stock_code'] in batch)
        batches.append((batch,first))
    phase='cycle_input_budget'
    try:
        for index,(batch,first) in enumerate(batches):
            remaining=120-(time.monotonic()-started)
            if remaining<=0:raise TimeoutError('cycle input budget exhausted')
            phase='training_inputs' if index==0 else f'outcome_inputs_{index}'
            detail={'purpose':phase,'since':str(first)};metrics['batches'].append(detail)
            got,calendar=read_inputs(conn,batch,first,as_of,cutoff,metrics=detail,budget_seconds=remaining)
            prices.extend(got)
            for row in calendar:
                day=row['trade_date']
                if day in calendar_by_day and row!=calendar_by_day[day]:
                    raise ValueError('calendar changed between outcome snapshots')
                calendar_by_day[day]=row
        prices.sort(key=lambda r:(r['stock_code'],r['dt']))
        metrics.update(status='complete',seconds=round(time.monotonic()-started,3),price_rows=len(prices))
        return prices,[calendar_by_day[d] for d in sorted(calendar_by_day)]
    except Exception as exc:
        metrics.update(status='failed',failed_phase=phase,partial_inputs_returned=False,
            seconds=round(time.monotonic()-started,3))
        raise LearningInputReadError(phase,metrics,exc) from exc


def record_failed_cycle(conn,started,as_of,codes,phase,exc,read_metrics,committed_progress=None):
    """Persist only minimal operational failure metadata at actual recording time."""
    summary={'status':'failed','as_of':str(as_of) if as_of else None,'started_at':started.isoformat(),
       'failure_phase':getattr(exc,'phase',phase),
       'error_type':getattr(exc,'original_error_type',type(exc).__name__),
       'error':'Learning cycle failed; see phase and error type. No SQL or credentials are recorded.',
       'production_approved':False,'models':[],'universe':list(dict.fromkeys(codes)),
       'input_read':read_metrics,'failure_recorded':False,
       'committed_progress':dict(committed_progress or {}),
       'partial_cycle_writes':any((committed_progress or {}).get(key,0)>0 for key in
          ('model_revisions_added','predictions_added','outcome_revisions_added')),
       'input_gap_scope':'unknown_on_failed_cycle',
       'models_scope':'Completed horizon status only; committed_progress also includes unfinished horizons.'}
    summary['models']=[dict(item) for item in (committed_progress or {}).get('completed_horizons',[])]
    if as_of is None:
        summary['failure_recording_error_type']='AsOfUnresolved'
        return summary
    try:
        # Failures are new system observations, never backdated to an old log.
        row=conn.execute("""INSERT INTO analytics.learning_cycles_v1
            (cycle_id,started_at,completed_at,as_of,status,summary)
            VALUES(%s,%s,clock_timestamp(),%s,'failed',%s) RETURNING completed_at""",
            (uuid.uuid4(),started,as_of,jsonb({**summary,'failure_recorded':True}))).fetchone()
        summary.update(failure_recorded=True,completed_at=str(row['completed_at']))
    except Exception as write_exc:
        summary['failure_recording_error_type']=type(write_exc).__name__
    return summary


def save_dataset(rows,key):
    path=repo_root()/'data'/'research_learning'/'datasets'/f'{key}.json.gz'
    path.parent.mkdir(parents=True,exist_ok=True)
    payload=canonical_json(rows).encode()
    if path.exists():
        with gzip.open(path,'rb') as f:
            if f.read()!=payload:
                raise ValueError('immutable dataset digest collision or damaged artifact')
    else:
        # Exclusive create prevents an interrupted writer from replacing a frozen artifact.
        tmp=path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        try:
            with gzip.open(tmp,'wb') as f: f.write(payload)
            os.replace(tmp,path)
        finally:
            tmp.unlink(missing_ok=True)
    return str(path)


def run_cycle(conn,as_of=None,codes=DEFAULT_CODES,horizons=(5,20),history_days=1500,dry_run=False):
    started=dt.datetime.now(dt.timezone.utc)
    conn.execute("SET statement_timeout='25s'")
    conn.execute("SET lock_timeout='3s'")
    locked=conn.execute('SELECT pg_try_advisory_lock(%s) AS ok',(LOCK_KEY,)).fetchone()['ok']
    if not locked:
        return {'status':'locked','reason':'另一个有限样本学习任务正在运行','production_approved':False}
    phase='preflight';read_metrics={};failure_exc=None;summary=None
    committed_progress={'model_revisions_added':0,'models_reused':0,'predictions_added':0,
                        'outcome_revisions_added':0,'completed_horizons':[]}
    try:
        latest=conn.execute("""SELECT max(trade_date) AS day FROM market.trading_calendar
            WHERE exchange='SSE' AND is_verified AND session_status='open'
              AND close_at<=%s AND available_at<=%s""",(started,started)).fetchone()['day']
        as_of=as_of or latest
        if as_of is None or as_of>latest:
            raise ValueError('as-of must be a completed verified trading session')
        codes=tuple(dict.fromkeys(codes));horizons=tuple(dict.fromkeys(horizons))
        if not 1<=len(codes)<=20 or any(len(c)!=6 or not c.isdigit() for c in codes):
            raise ValueError('provide 1 to 20 explicit six-digit stock codes')
        if not set(horizons)<= {5,20,60} or not horizons: raise ValueError('horizons must be 5,20,60')
        if not 800<=history_days<=1800: raise ValueError('history-days must be 800..1800')
        exists=conn.execute("SELECT to_regclass('analytics.learning_models_v1') AS t").fetchone()['t']
        if exists is None: raise RuntimeError('install migrate_forecast_learning_v1.sql before running')
        # Always settle pending forecasts, even if the next caller changes its training universe.
        phase='pending_outcomes'
        pending=conn.execute("""SELECT p.* FROM analytics.learning_predictions_v1 p
            WHERE p.exit_date<=%s AND p.entry_date>=%s
              AND (NOT EXISTS(SELECT 1 FROM analytics.learning_outcomes_v1 o WHERE o.prediction_id=p.prediction_id)
                   OR p.exit_date >= %s)
            ORDER BY EXISTS(SELECT 1 FROM analytics.learning_outcomes_v1 o WHERE o.prediction_id=p.prediction_id),
                     p.exit_date DESC,p.created_at LIMIT 5000""",
            (as_of,as_of-dt.timedelta(days=history_days),as_of-dt.timedelta(days=120))).fetchall()
        phase='input_read'
        prices,calendar=read_cycle_inputs(conn,codes,pending,as_of-dt.timedelta(days=history_days),as_of,started,read_metrics)
        if as_of not in {r['trade_date'] for r in calendar}: raise ValueError('as-of is not an open calendar date')
        summary={'as_of':str(as_of),'cutoff':str(started),'knowledge_mode':'public_reconstruction',
                 'universe':list(codes),'price_rows_loaded':len(prices),'production_approved':False,
                 'models':[],'predictions_added':0,'outcome_revisions_added':0,'limitations':LIMITATIONS,
                 'input_read':read_metrics,'settlement_candidates':len(pending),'settlement_backlog_may_remain':len(pending)==5000,
                 'outcome_correction_review_days':120,
                 'forward_recording_allowed':as_of==latest}
        phase='settle_outcomes'
        for p in pending:
            result=settle_prediction(p,prices,calendar,started)
            if result:
                old=conn.execute('SELECT 1 FROM analytics.learning_outcomes_v1 WHERE prediction_id=%s AND source_digest=%s',
                                 (p['prediction_id'],result['source_digest'])).fetchone()
                if not old:
                    if not dry_run:
                        written=conn.execute("INSERT INTO analytics.learning_outcomes_v1 VALUES(%s,%s,clock_timestamp(),%s,%s) ON CONFLICT DO NOTHING",
                                     (uuid.uuid4(),p['prediction_id'],result['source_digest'],jsonb(result)))
                        committed_progress['outcome_revisions_added']+=max(0,written.rowcount)
                        summary['outcome_revisions_added']+=max(0,written.rowcount)
                    else:summary['outcome_revisions_added']+=1
        selected_prices=[p for p in prices if p['stock_code'] in codes]
        phase='model_refresh'
        for h in horizons:
            phase=f'model_horizon_{h}_prepare'
            rows,current,issues=build_examples(selected_prices,calendar,h,as_of,started)
            stable_rows=[{k:v for k,v in r.items() if k not in ('feature_available_at','label_available_at')}
                         for r in rows]
            key=digest({'contract':CONTRACT,'version':VERSION,'universe':list(codes),'horizon':h,'rows':stable_rows})
            stats={'rows':len(rows),'dates':len({r['signal_date'] for r in rows}),
                   'stocks':len({r['stock_code'] for r in rows}),'stock_codes':list(codes),
                   'earliest_signal':min((r['signal_date'] for r in rows),default=None),
                   'latest_signal':max((r['signal_date'] for r in rows),default=None),
                   'outcomes_through':max((r['exit_date'] for r in rows),default=None),
                   'source_quality':dict(__import__('collections').Counter(p['quality_status'] for p in selected_prices)),
                   'input_knowledge_cutoff':str(started),
                   'input_contract':CONTRACT}
            item={'horizon':h,'input_stats':stats,'current_inputs':issues}
            old=conn.execute("""SELECT * FROM analytics.learning_models_v1
                WHERE model_version=%s AND horizon=%s AND input_digest=%s""",(VERSION,h,key)).fetchone()
            if old:
                model=old;item['status']='reused'
                committed_progress['models_reused']+=1
            else:
                phase=f'model_horizon_{h}_training'
                try: trained=train_research(rows,h)
                except ValueError as exc:
                    item.update(status='insufficient_data',reason=str(exc));summary['models'].append(item)
                    committed_progress['completed_horizons'].append({'horizon':h,'status':'insufficient_data'})
                    continue
                model={'model_id':uuid.uuid4(),'parameters':trained['parameters'],'evaluation':trained['evaluation']}
                path='dry-run'
                if not dry_run:
                    phase=f'model_horizon_{h}_write'
                    path=save_dataset(stable_rows,key)
                    conn.execute("""INSERT INTO analytics.learning_models_v1
                        (model_id,model_version,horizon,as_of,created_at,input_digest,knowledge_mode,status,input_stats,parameters,evaluation,dataset_path)
                        VALUES(%s,%s,%s,%s,clock_timestamp(),%s,'public_reconstruction','research',%s,%s,%s,%s)""",
                        (model['model_id'],VERSION,h,as_of,key,jsonb(stats),jsonb(model['parameters']),jsonb(model['evaluation']),path))
                    committed_progress['model_revisions_added']+=1
                item['status']='trained'
            item['evaluation']=model['evaluation']
            item['refreshed_trained_through']=model['parameters']['trained_outcome_through']
            if as_of==latest and current:
                phase=f'model_horizon_{h}_predictions'
                estimates=predict(model['parameters'],current)
                lower,upper=prediction_intervals(model['parameters'],current,estimates)
                for feature,estimate,low,high in zip(current,estimates,lower,upper):
                    entry=dt.date.fromisoformat(feature['entry_date'])
                    recorded=dt.datetime.now(dt.timezone.utc)
                    # A forward prediction must actually be recorded before its entry open.
                    if not can_record_forward(as_of,latest,dt.datetime.fromisoformat(feature['entry_at']),recorded):
                        continue
                    frozen={**feature,'contract':CONTRACT,'production_approved':False,
                            'input_knowledge_cutoff':str(started),'computed_at':str(recorded),'research_only':True}
                    if not dry_run:
                        # JSONB adapters do not serialize date/datetime automatically.
                        inserted=conn.execute(PREDICTION_INSERT_SQL,
                            (uuid.uuid4(),model['model_id'],feature['stock_code'],feature['name'],as_of,h,entry,
                             dt.date.fromisoformat(feature['exit_date']),float(estimate),jsonb([float(low),float(high)]),
                             feature['x'][1]*h/20,model['evaluation']['research_validation_passed'],jsonb(json.loads(canonical_json(frozen))),
                             dt.datetime.fromisoformat(feature['entry_at']))).fetchone()
                        summary['predictions_added']+=bool(inserted)
                        committed_progress['predictions_added']+=bool(inserted)
                    else: summary['predictions_added']+=1
            summary['models'].append(item)
            committed_progress['completed_horizons'].append({'horizon':h,'status':item['status']})
        summary['committed_progress']=committed_progress
        trained=any(m['status']=='trained' for m in summary['models'])
        has_model=any(m['status'] in ('trained','reused') for m in summary['models'])
        changed=trained or summary['predictions_added'] or summary['outcome_revisions_added']
        summary['status']='run' if changed else ('idempotent' if has_model else 'insufficient_data')
        summary['dry_run']=dry_run
        if not dry_run:
            phase='cycle_record'
            conn.execute('INSERT INTO analytics.learning_cycles_v1 VALUES(%s,%s,%s,%s,%s,%s)',
                         (uuid.uuid4(),started,dt.datetime.now(dt.timezone.utc),as_of,summary['status'],jsonb(summary)))
        return summary
    except Exception as exc:
        failure_exc=exc
        if not dry_run:
            exc.learning_failure=record_failed_cycle(conn,started,as_of,codes,phase,exc,read_metrics,committed_progress)
        raise
    finally:
        try:conn.execute('SELECT pg_advisory_unlock(%s)',(LOCK_KEY,))
        except Exception as unlock_exc:
            if failure_exc is None:
                # A completed business cycle remains successful. Its dedicated
                # caller must close the connection to release any session lock.
                if summary is not None:
                    summary.setdefault('cleanup_warnings',[]).append({
                        'phase':'session_unlock','error_type':type(unlock_exc).__name__,
                        'session_lock_release':'requires_connection_close'})
                else:raise
            # Preserve the original failure when connection cleanup also fails.
            if hasattr(failure_exc,'learning_failure'):
                failure_exc.learning_failure['unlock_error_type']=type(unlock_exc).__name__


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of',type=dt.date.fromisoformat)
    parser.add_argument('--stock-codes',default=','.join(DEFAULT_CODES))
    parser.add_argument('--horizons',default='5,20')
    parser.add_argument('--history-days',type=int,default=1500)
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    try:
        with connect() as conn:
            result=run_cycle(conn,args.as_of,tuple(args.stock_codes.split(',')),tuple(map(int,args.horizons.split(','))),args.history_days,args.dry_run)
    except Exception as exc:
        result=getattr(exc,'learning_failure',None) or {
            'status':'failed','error_type':getattr(exc,'original_error_type',type(exc).__name__),
            'failure_phase':getattr(exc,'phase','startup'),'input_read':getattr(exc,'read_metrics',{}),
            'error':'Learning cycle failed; see phase and error type.','production_approved':False,
            'failure_recorded':False}
        if args.report: args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(canonical_json(result)+'\n')
        print(canonical_json(result));return 1
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
    print(canonical_json(result));return 0

if __name__=='__main__':sys.exit(main())
