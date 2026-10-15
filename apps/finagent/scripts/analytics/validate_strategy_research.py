"""Freeze candidates on development; open validation and final only in order."""
from __future__ import annotations
import argparse,dataclasses,gzip,json,datetime
from pathlib import Path
from strategy_dataset import dump,checksum
from strategy_research import Policy,load_dataset,prepare_features,backtest,paired_comparison,research_gate,balanced_score
from strategy_inference import paired_block_interval,wilson,familywise_development_test


def save_run(p,r,baseline):
    p.parent.mkdir(parents=True,exist_ok=True)
    with gzip.open(p,'wt') as f:json.dump(r,f,allow_nan=False)
    comparison=paired_comparison(r,baseline);m=r['metrics']
    return {'name':r['name'],'policy':r['policy'],'metrics':m,'comparison':comparison,
        'gate':research_gate(r,comparison),'excess_uncertainty':paired_block_interval(r,baseline),
        'window_win_wilson_95':wilson(m['winning_periods'],m['period_count']),
        'baseline_quality':{'terminal_stale_value':baseline['metrics']['terminal_stale_value'],
          'maximum_held_stale_sessions':baseline['metrics']['maximum_held_stale_sessions'],
          'held_anomalies':baseline['metrics']['held_anomalies']},'artifact':str(p)}


def freeze(dataset,runs,output):
    p=Path(output);p.mkdir(exist_ok=False,parents=True)
    candidates=[];paths=[]
    for run in runs:
        for row in json.loads((Path(run)/'all-trials.json').read_text()):
            if row.get('failed'):continue
            candidates.append(row);paths.append(Path(run)/(row['name']+'.json.gz'))
    dump(p/'SELECTION_RULE.json',{'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'development':'2021..2024','candidate_count':len(candidates),'at_most':2,'distinct_families':True,
        'rule':'development research_gate; positive net CAGR after 30bp one-way fees and after 2-session signal lag; highest fixed balanced_score, one per family',
        'validation_rule':'same gate on discovery 2025; positive net profit under fee/delay stress; cross-section reserve positive 2021..2025 excess',
        'final_rule':'freeze best validation score among qualified candidates; if none qualify, do not open final test',
        'no_more_tuning_after_validation':True,'dataset_sha256':checksum(Path(dataset)/'manifest.json'),
        'simulator_sha256':checksum(Path(__file__).with_name('strategy_research.py'))})
    d=load_dataset(dataset,until='2024-12-31');f=prepare_features(d);bases={};retained=[];source_runs=[];base_runs=[]
    for row,path in zip(candidates,paths):
        with gzip.open(path,'rt') as stream:r=json.load(stream)
        h=row['policy']['rebalance']
        if (h,15.,1) not in bases:bases[h,15.,1]=backtest(d,f,Policy('equal_weight',rebalance=h,size=len(d['symbols']),round_lots=False),'2021-01-01','2024-12-31')
        source_runs.append(r);base_runs.append(bases[h,15.,1])
        if not row['research_gate']['passed']:continue
        policy=Policy(**row['policy']);stress=[]
        for label,changes in [('double_cost',{'cost_bps':30.}),('delay_one_extra_day',{'signal_lag':2})]:
            tested=dataclasses.replace(policy,**changes)
            key=h,tested.cost_bps,tested.signal_lag
            if key not in bases:bases[key]=backtest(d,f,Policy('equal_weight',rebalance=h,size=len(d['symbols']),round_lots=False,cost_bps=tested.cost_bps,signal_lag=tested.signal_lag),'2021-01-01','2024-12-31')
            sr=backtest(d,f,tested,'2021-01-01','2024-12-31')
            stress.append({'scenario':label,**save_run(p/'development-stress'/(row['name']+'-'+label+'.json.gz'),sr,bases[key])})
        retained.append({**row,'stress':stress,'stress_eligible':all(x['metrics']['cagr']>0 for x in stress)})
    dump(p/'ALL_DEVELOPMENT_STRESS.json',retained)
    dump(p/'MULTIPLE_TESTING.json',familywise_development_test(source_runs,base_runs))
    chosen=[];families=set()
    for row in sorted(retained,key=lambda r:r['balanced_score'],reverse=True):
        if not row['stress_eligible'] or row['policy']['family'] in families:continue
        chosen.append(row);families.add(row['policy']['family'])
        if len(chosen)==2:break
    dump(p/'CANDIDATES_FROZEN.json',{'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'candidates':chosen,
        'validation_opened':False,'final_opened':False,'production_approved':False})
    print(json.dumps({'frozen':[r['name'] for r in chosen],'development_gate_passes':len(retained)}),flush=True)


def validate(dataset,output):
    p=Path(output);rule=json.loads((p/'SELECTION_RULE.json').read_text())
    if checksum(Path(dataset)/'manifest.json')!=rule['dataset_sha256']:raise ValueError('data changed')
    if checksum(Path(__file__).with_name('strategy_research.py'))!=rule['simulator_sha256']:raise ValueError('simulator changed after freeze')
    if (p/'VALIDATION.json').exists():raise ValueError('validation already opened; no reselection')
    frozen=json.loads((p/'CANDIDATES_FROZEN.json').read_text())['candidates']
    (p/'validation').mkdir(exist_ok=False)
    dump(p/'VALIDATION_OPENED.json',{'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'frozen_sha256':checksum(p/'CANDIDATES_FROZEN.json')})
    discovery=load_dataset(dataset,until='2025-12-31');df=prepare_features(discovery)
    reserve=load_dataset(dataset,cohort='cross_section_reserve',until='2025-12-31');rf=prepare_features(reserve)
    results=[]
    for row in frozen:
        policy=Policy(**row['policy']);scenarios={}
        for label,changes in [('normal',{}),('double_cost',{'cost_bps':30.}),('delay_one_extra_day',{'signal_lag':2})]:
            tested=dataclasses.replace(policy,**changes)
            r=backtest(discovery,df,tested,'2025-01-01','2025-12-31',keep_trades=True)
            b=backtest(discovery,df,Policy('equal_weight',rebalance=policy.rebalance,size=len(discovery['symbols']),round_lots=False,
                cost_bps=tested.cost_bps,signal_lag=tested.signal_lag),'2025-01-01','2025-12-31')
            dump(p/'validation'/('baseline-'+row['name']+'-'+label+'.json'),b)
            scenarios[label]=save_run(p/'validation'/(row['name']+'-'+label+'.json.gz'),r,b)
        rr=backtest(reserve,rf,policy,'2021-01-01','2025-12-31',keep_trades=True)
        rb=backtest(reserve,rf,Policy('equal_weight',rebalance=policy.rebalance,size=len(reserve['symbols']),round_lots=False),'2021-01-01','2025-12-31')
        reserve_summary=save_run(p/'validation'/(row['name']+'-reserve.json.gz'),rr,rb)
        dump(p/'validation'/('baseline-'+row['name']+'-reserve.json'),rb)
        m=scenarios['normal'];checks={'discovery_gate':m['gate']['passed'],
          'stress_positive':all(scenarios[x]['metrics']['net_return']>0 for x in ('double_cost','delay_one_extra_day')),
          'reserve_positive_excess':reserve_summary['comparison']['cagr_excess']>0,
          'reserve_positive_profit':reserve_summary['metrics']['net_return']>0,
          'reserve_no_held_anomaly':not reserve_summary['metrics']['held_anomalies'],
          'reserve_no_terminal_stale':reserve_summary['metrics']['terminal_stale_value']<.01,
          'reserve_no_long_gap':reserve_summary['metrics']['maximum_held_stale_sessions']<=20}
        results.append({'name':row['name'],'policy':row['policy'],'discovery':scenarios,'reserve':reserve_summary,
          'checks':checks,'passed':all(checks.values()),'selection_score':balanced_score({'metrics':m['metrics']},m['comparison'])})
    dump(p/'VALIDATION.json',{'candidates':results,'final_opened':False,'production_approved':False})
    passing=sorted([r for r in results if r['passed']],key=lambda r:r['selection_score'],reverse=True)
    dump(p/'FINAL_FROZEN.json',{'candidate':passing[0] if passing else None,'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
       'validation_sha256':checksum(p/'VALIDATION.json'),'reason':'best predeclared validation score' if passing else 'no qualified validation candidate; final remains unopened'})
    print(json.dumps({'validation':[{'name':r['name'],'passed':r['passed'],'checks':r['checks']} for r in results],'final_candidate':passing[0]['name'] if passing else None}),flush=True)


def final(dataset,output):
    p=Path(output);rule=json.loads((p/'SELECTION_RULE.json').read_text())
    if checksum(Path(dataset)/'manifest.json')!=rule['dataset_sha256'] or checksum(Path(__file__).with_name('strategy_research.py'))!=rule['simulator_sha256']:raise ValueError('frozen inputs changed')
    if (p/'FINAL.json').exists():raise ValueError('final already opened')
    candidate=json.loads((p/'FINAL_FROZEN.json').read_text())['candidate']
    if not candidate:raise ValueError('no qualified validation candidate; do not open final')
    (p/'final').mkdir(exist_ok=False)
    dump(p/'FINAL_OPENED.json',{'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'candidate_sha256':checksum(p/'FINAL_FROZEN.json')})
    d=load_dataset(dataset);f=prepare_features(d);policy=Policy(**candidate['policy']);scenarios={}
    for label,changes in [('normal',{}),('double_cost',{'cost_bps':30.}),('delay_one_extra_day',{'signal_lag':2})]:
        tested=dataclasses.replace(policy,**changes)
        r=backtest(d,f,tested,'2026-01-01','2026-10-13',keep_trades=True)
        b=backtest(d,f,Policy('equal_weight',rebalance=policy.rebalance,size=len(d['symbols']),round_lots=False,
           cost_bps=tested.cost_bps,signal_lag=tested.signal_lag),'2026-01-01','2026-10-13')
        dump(p/'final'/('baseline-'+label+'.json'),b)
        scenarios[label]=save_run(p/'final'/(label+'.json.gz'),r,b)
    passed=scenarios['normal']['gate']['passed'] and all(scenarios[k]['metrics']['net_return']>0 for k in ('double_cost','delay_one_extra_day'))
    dump(p/'FINAL.json',{'candidate':candidate['name'],'scenarios':scenarios,'research_gate_passed':passed,
      'production_approved':False,'reason':'research reconstruction is not a production PIT qualification'})
    print(json.dumps({'candidate':candidate['name'],'research_gate_passed':passed,'production_approved':False}),flush=True)


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('stage',choices=['freeze','validate','final']);ap.add_argument('--dataset',required=True)
    ap.add_argument('--output',required=True);ap.add_argument('--runs',nargs='*');args=ap.parse_args()
    if args.stage=='freeze':freeze(args.dataset,args.runs,args.output)
    elif args.stage=='validate':validate(args.dataset,args.output)
    else:final(args.dataset,args.output)
