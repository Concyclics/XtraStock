"""Bounded price-factor discovery on reconstructed history, never factor promotion."""
from __future__ import annotations
import datetime as dt
import hashlib
import itertools
import json
import sys
from pathlib import Path
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.analytics.common import connect,jsonb
from scripts.analytics.research_history import history_plan
from scripts.analytics.factor_validation import purged_folds
from scripts.analytics.forecast_learning import build_examples,FEATURES
from scripts.analytics.forecast_validation import ranked_backtest,block_interval

VERSION='reconstructed-factor-v2-stability'

def select_stable_factors(daily,train,names):
    """Choose on three chronological TRAIN subperiods, deduplicate TRAIN ICs."""
    parts=np.array_split(train,3);scored=[]
    for name in names:
        history=[daily[d][name]['ic'] for d in train]
        finite=[v for v in history if v is not None]
        if len(finite)<max(30,int(.8*len(train))):continue
        mean=float(np.mean(finite));direction=1 if mean>=0 else -1
        means=[float(np.mean([daily[d][name]['ic'] for d in part if daily[d][name]['ic'] is not None]))
               for part in parts if any(daily[d][name]['ic'] is not None for d in part)]
        consistency=sum(v*direction>0 for v in means)
        score=abs(mean)-.5*float(np.std(means))
        scored.append({'name':name,'direction':direction,'score':score,'training_rank_ic':mean,
            'training_subperiod_ics':means,'consistent_subperiods':consistency,
            'eligible':len(means)==3 and consistency>=2 and score>0})
    ranked=sorted(scored,key=lambda item:(-item['score'],item['name']))
    selected=[];rejected=[]
    for item in ranked:
        if not item['eligible']:continue
        duplicate=None
        for previous in selected:
            pairs=[(daily[d][item['name']]['ic'],daily[d][previous['name']]['ic']) for d in train
                   if daily[d][item['name']]['ic'] is not None and daily[d][previous['name']]['ic'] is not None]
            a,b=np.asarray(pairs).T
            if len(pairs)>20 and np.std(a)>1e-10 and np.std(b)>1e-10 and abs(np.corrcoef(a,b)[0,1])>.9:
                duplicate=previous['name'];break
        if duplicate:
            rejected.append({'name':item['name'],'correlated_with':duplicate});continue
        selected.append(item)
        if len(selected)==3:break
    return selected,ranked,rejected

def evaluate_rows(rows,horizon,plan,min_assets=8,max_candidates=16):
    """Tune candidate and orientation on training dates; evaluate unchanged OOS."""
    by_date={};keys=set()
    for r in rows:by_date.setdefault(r['signal_date'],[]).append(r)
    for r in rows:
        key=(r['stock_code'],r['signal_date'])
        if key in keys:raise ValueError('duplicate stock/date factor rows')
        if not np.isfinite([*r['x'],r['y']]).all():raise ValueError('non-finite factor inputs')
        keys.add(key)
    by_date={d:rs for d,rs in by_date.items() if len({r['stock_code'] for r in rs})>=min_assets}
    dates=sorted(by_date)
    windows=purged_folds(dates,folds=plan['folds'],test_sessions=plan['test_sessions'],gap=horizon,min_train_sessions=plan['min_train_sessions'])
    result={'status':'insufficient_history','eligible_sessions':len(dates),'required_sessions':plan['required_eligible_sessions'],
        'missing_sessions':max(0,plan['required_eligible_sessions']-len(dates)),'folds':[],
        'knowledge_mode':'public_reconstruction','production_approved':False,'candidate_definitions_written':0,
        'universe_policy':'当前候选池的回溯研究，含选择偏差；不可当作历史全市场策略收益'}
    if len(windows)<plan['folds']:return result
    definitions=[(name,(i,),1) for i,name in enumerate(FEATURES)]
    for i,j in itertools.combinations(range(len(FEATURES)),2):
        for sign,label in ((1,'plus'),(-1,'minus')):definitions.append((FEATURES[i]+'_'+label+'_'+FEATURES[j],(i,j),sign))
    definitions=definitions[:max_candidates]
    daily={};daily_ranks={}
    def ranks(a):
        # Average ties, matching rank IC rather than inventing an order for ties.
        order=np.argsort(a,kind='stable');out=np.empty(len(a),dtype=float);pos=0
        while pos<len(a):
            end=pos+1
            while end<len(a) and a[order[end]]==a[order[pos]]:end+=1
            out[order[pos:end]]=(pos+end-1)/2;pos=end
        return out
    for d,rs in by_date.items():
        x=np.array([r['x'] for r in rs]);y=np.array([r['y'] for r in rs]);std=x.std(axis=0)
        z=(x-x.mean(axis=0))/np.where(std>0,std,1);ry=ranks(y);values={};rank_values={}
        for name,index,sign in definitions:
            v=z[:,index[0]] if len(index)==1 else z[:,index[0]]+sign*z[:,index[1]]
            rv=ranks(v);ic=float(np.corrcoef(rv,ry)[0,1]) if rv.std()>0 and ry.std()>0 else None
            values[name]={'ic':ic,'spread':float(y[rv>=np.quantile(rv,.8)].mean()-y[rv<=np.quantile(rv,.2)].mean()) if rv.std()>0 else None}
            rank_values[name]=rv/max(1,len(rv)-1)
        daily[d]=values;daily_ranks[d]=rank_values
    for start,end,test_start,test_end in windows:
        # Purge by actual exit dates too, since dates with insufficient stocks may
        # be absent from the date list. No outcome may overlap the next phase.
        train=[d for d in dates if start<=d<=end and max(r['exit_date'] for r in by_date[d])<test_start]
        test=[d for d in dates if test_start<=d<=test_end]
        if len(train)<plan['min_train_sessions']:continue
        scored=[]
        for name,_,_ in definitions:
            ics=[daily[d][name]['ic'] for d in train if daily[d][name]['ic'] is not None]
            if not ics:continue
            mean=float(np.mean(ics));scored.append((abs(mean),name,1 if mean>=0 else -1))
        if not scored:continue
        _,old_chosen,old_direction=max(scored,key=lambda a:(a[0],a[1]))
        selected,training_scores,redundant=select_stable_factors(daily,train,[n for n,_,_ in definitions])
        oos=[];test_rows=[];test_scores=[]
        for d in test:
            if not selected:continue
            score=np.mean([daily_ranks[d][f['name']]*f['direction'] for f in selected],axis=0)
            ry=ranks(np.array([r['y'] for r in by_date[d]]));rv=ranks(score)
            if ry.std()==0 or rv.std()==0:continue
            y=np.array([r['y'] for r in by_date[d]])
            oos.append({'ic':float(np.corrcoef(rv,ry)[0,1]),
                'spread':float(y[rv>=np.quantile(rv,.8)].mean()-y[rv<=np.quantile(rv,.2)].mean())})
            test_rows.extend(by_date[d]);test_scores.extend(score.tolist())
        chosen=selected[0]['name'] if selected else None;direction=selected[0]['direction'] if selected else None
        has_entries=test_rows and all('entry_date' in r for r in test_rows)
        backtest=ranked_backtest(test_rows,test_scores) if has_entries else {'status':'unavailable','reason':'no_selected_factor_or_missing_entry_dates'}
        legacy_ics=[daily[d][old_chosen]['ic']*old_direction for d in test if daily[d][old_chosen]['ic'] is not None]
        result['folds'].append({'train_start':start,'train_end':end,'test_start':test_start,'test_end':test_end,
            'training_dates':len(train),'test_dates':len(oos),'chosen_expression':chosen,'direction':direction,
            'selected_factors':selected,'training_candidate_scores':training_scores,'correlation_rejections':redundant,
            'test_rank_ic':float(np.mean([v['ic'] for v in oos])) if oos else None,
            'test_rank_ic_interval':block_interval([v['ic'] for v in oos],horizon) if oos else None,
            'test_top_bottom_spread':float(np.mean([v['spread'] for v in oos])) if oos else None,
            'legacy_test_rank_ic':float(np.mean(legacy_ics)) if legacy_ics else None,
            'backtest':backtest,'cost_stress':ranked_backtest(test_rows,test_scores,50) if has_entries else None,
            'costs_included':False,'purpose':'价差本身不含费用；backtest 单独报告费用情景，未模拟真实成交'})
    result.update(status='complete' if len(result['folds'])==plan['folds'] and
        all(f['test_dates']>=plan['test_sessions'] for f in result['folds']) else 'insufficient_variation',
        candidates_tested=[n for n,_,_ in definitions],selection_rule='training_subperiod_stability_and_correlation_pruning',
        positive_test_folds=sum((f['test_rank_ic'] or 0)>0 for f in result['folds']),
        research_signal_validated=len(result['folds'])>=3 and len(result['folds'])==plan['folds'] and
            all(f['test_dates']>=max(plan['test_sessions'],5*horizon) and f['test_rank_ic_interval'] and
                f['test_rank_ic_interval']['lower_95']>0 for f in result['folds']),
        validation_minimums={'folds':3,'test_sessions_per_fold':max(plan['test_sessions'],5*horizon),
            'rank_ic_lower_95_above_zero_in_every_fold':True},
        validation_policy='每折仅训练段选表达式和方向；测试段不参与选择；所有折保留，未授予生产资格')
    return result

def run_reconstruction(cfg,dry_run=False):
    # Import the existing serial, timed price reader without changing its contract.
    analytics=str(ROOT/'scripts/analytics')
    if analytics not in sys.path:sys.path.insert(0,analytics)
    from run_forecast_learning_cycle import read_inputs
    now=dt.datetime.now(dt.timezone.utc);m=cfg['mining']
    with connect(attempts=1) as c:
        latest=c.execute("""SELECT max(trade_date) d FROM market.trading_calendar WHERE exchange='SSE'
            AND is_verified AND session_status='open' AND close_at<=%s AND available_at<=%s""",(now,now)).fetchone()['d']
        run=c.execute('SELECT run_id FROM analytics.candidate_pool_runs WHERE created_at<=%s ORDER BY created_at DESC LIMIT 1',(now,)).fetchone()
        if not run:return {'status':'missing_candidate_pool','launched_training':False}
        codes=[r['stock_code'] for r in c.execute("""SELECT stock_code FROM analytics.candidate_pool_revisions
            WHERE run_id=%s AND state IN ('core','watching','exploring') ORDER BY rank LIMIT %s""",(run['run_id'],m['max_stocks']))]
        if not 8<=m['max_stocks']<=40 or not 5<=m['min_assets_per_date']<=m['max_stocks']:raise ValueError('invalid reconstruction cohort limits')
        if not 365<=m['history_days']<=1800:raise ValueError('history_days must be 365..1800')
        if not 1<=m['max_candidates']<=25:raise ValueError('max_candidates must be 1..25')
        plans={h:history_plan(m['min_train_sessions'],m['test_sessions'],m['folds'],h) for h in m['horizons']}
        if not plans:raise ValueError('at least one horizon required')
        if dry_run:return {'status':'planned','mode':'public_reconstruction','as_of':str(latest),'stock_count':len(codes),'plans':plans,'launched_training':False,'config':m}
        if not c.execute("SELECT pg_try_advisory_lock(704201352) ok").fetchone()['ok']:return {'status':'locked','launched_training':False}
        read={};prices,calendar=read_inputs(c,codes,latest-dt.timedelta(days=m['history_days']),latest,now,metrics=read)
        datasets={};hash_inputs=[]
        for h,plan in plans.items():
            rows,_,_=build_examples(prices,calendar,h,latest,now)
            datasets[h]=rows
            hash_inputs.append({'horizon':h,'rows':[{k:r[k] for k in ('stock_code','signal_date','entry_date','exit_date','x','y')} for r in rows]})
        key=hashlib.sha256(json.dumps({'version':VERSION,'codes':codes,'config':m,'data':hash_inputs},sort_keys=True,default=str).encode()).hexdigest()
        old=c.execute('SELECT summary FROM analytics.factor_reconstruction_runs WHERE input_fingerprint=%s',(key,)).fetchone()
        if old:
            return {**old['summary'],'reused':True,'launched_training':False,'input_read':read}
        results=[{'horizon':h,'plan':plan,**evaluate_rows(datasets[h],h,plan,m['min_assets_per_date'],m['max_candidates'])}
                 for h,plan in plans.items()]
        summary={'status':'complete' if all(r['status']=='complete' for r in results) else 'partial',
            'mode':'public_reconstruction','version':VERSION,'as_of':str(latest),'stock_codes':codes,'results':results,
            'input_read':read,'production_approved':False,'launched_training':True}
        c.execute('''INSERT INTO analytics.factor_reconstruction_runs(as_of_date,input_fingerprint,config,summary)
            VALUES(%s,%s,%s,%s) ON CONFLICT(input_fingerprint) DO NOTHING''',(latest,key,jsonb(m),jsonb(summary)))
        # The connection context releases the session lock even on read/training error.
        return summary
