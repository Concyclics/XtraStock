"""Freeze and run five-year valuation/earnings daily-limit research comparisons.

All variants are reported. This study estimates no probability or target-hit
date and cannot turn approximate historical execution into an approval.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import csv
import gzip
import json
from pathlib import Path
import shutil
import numpy as np
try:
    from .strategy_dataset import checksum,dump
    from .strategy_research import load_dataset,prepare_features,rank
    from .forecast_research_v3 import forecast_features,public_date
    from .value_limit_replay import replay,MAINBOARD
    from .value_plan import load_config,qualify
except ImportError:
    from strategy_dataset import checksum,dump
    from strategy_research import load_dataset,prepare_features,rank
    from forecast_research_v3 import forecast_features,public_date
    from value_limit_replay import replay,MAINBOARD
    from value_plan import load_config,qualify

def signals(d,f,source,cfg):
    forecasts,audit=forecast_features(d,source,current_year_only=True)
    dates=d['dates'];codes={str(c):j for j,c in enumerate(d['symbols'])}
    industry_events=[]
    for r in source:
        if r.get('industry') and r['stock_code'] in codes:
            industry_events.append((public_date(r['report_date'],r.get('published_at'),r.get('eastmoney_infocode')),
                codes[r['stock_code']],r['industry']))
    industry_events=sorted(set(industry_events));industry=['']*len(codes);at=0
    ready=np.isfinite(forecasts['value'])&(forecasts['value']>0)&(forecasts['value_brokers']>=cfg['min_institutions'])&f['eligible']
    ready &= forecasts['nonpositive_brokers']==0
    ready &= np.array([str(s).startswith(MAINBOARD) for s in d['symbols']])[None,:]
    fair=np.full(d['close'].shape,np.nan);peer_count=np.zeros(d['close'].shape,np.int16)
    for t,day in enumerate(dates):
        while at<len(industry_events) and industry_events[at][0]<=day:
            _,j,sector=industry_events[at];industry[j]=sector;at+=1
        groups=defaultdict(list)
        for j in np.flatnonzero(ready[t]):
            sector=industry[j];pe=1/forecasts['value'][t,j]
            if sector and not any(x in sector for x in ('银行','保险','证券','多元金融')) and pe<=cfg['valuation_pe_cap']:
                groups[sector].append((j,pe))
        for j in np.flatnonzero(ready[t]):
            group=[pe for k,pe in groups.get(industry[j],[]) if k!=j]
            peer_count[t,j]=len(group)
            if len(group)>=cfg['min_industry_peers']:
                fair[t,j]=d['close'][t,j]*forecasts['value'][t,j]*min(float(np.median(group)),cfg['valuation_pe_cap'])
    # Main study uses dated sector-relative valuation. The absolute-PE control
    # is separately named; it cannot stand in for missing sector history.
    revision=forecasts['revision'];known_revision=np.isfinite(revision)&(forecasts['revision_brokers']>=1)
    relative=ready&np.isfinite(fair)&known_revision&(revision>=0)
    absolute=ready&known_revision&(revision>=0)&(forecasts['value']>=.05)
    value_rank=rank(forecasts['value'],ready);revision_rank=rank(revision,ready&known_revision)
    score=.5*value_rank+.3*revision_rank+.2*f['score']['momentum_low_vol']
    relative_limit=np.minimum(fair*(1-cfg['entry_discount']),d['close']*(1-cfg['no_chase_discount']))
    absolute_fair=d['close']*forecasts['value']*20
    absolute_limit=np.minimum(absolute_fair*(1-cfg['entry_discount']),d['close']*(1-cfg['no_chase_discount']))
    return {
        'sector_value_revision':(np.where(relative,score,np.nan),relative_limit),
        'absolute_pe_revision_control':(np.where(absolute,score,np.nan),absolute_limit),
        'matched_equal_weight':(np.where(relative,1.,np.nan),d['close']*.99),
        'matched_momentum':(np.where(relative,f['score']['momentum_low_vol'],np.nan),d['close']*.99),
        'absolute_equal_weight':(np.where(absolute,1.,np.nan),d['close']*.99),
        'absolute_momentum':(np.where(absolute,f['score']['momentum_low_vol'],np.nan),d['close']*.99),
    }, {**audit,'historical_sector_events':len(industry_events),'sector_ready_days':int(relative.sum()),
        'absolute_control_ready_days':int(absolute.sum()),'eligible_stocks':int(relative.any(axis=0).sum()),
        'by_year':{year:{'sector_ready_stock_days':int(relative[np.char.startswith(dates,year)].sum()),
                        'absolute_ready_stock_days':int(absolute[np.char.startswith(dates,year)].sum())}
                   for year in sorted({str(day)[:4] for day in dates if day>=d.get('primary_since',str(dates[0]))})}}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--dataset',required=True,type=Path)
    p.add_argument('--earnings-source',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();out=a.output;out.mkdir(parents=True,exist_ok=True);cfg=load_config()
    manifest=json.loads((a.dataset/'manifest.json').read_text());start=manifest['primary_since'];end=manifest['until']
    if not start or not end or int(end[:4])-int(start[:4])!=5:raise ValueError('recent five-year dataset required')
    spec={'version':'daily-value-limit-study-v1','primary_since':start,'until':end,'config':cfg,
        'inputs':{k:checksum(a.dataset/k) for k in ('market.npz','assets.json','actions.json','manifest.json')},
        'earnings_source_sha256':checksum(a.earnings_source),'cohorts':['all','cross_section_reserve'],
        'horizons':[20,60],'cost_bps_per_side':[15,30],
        'variants':['sector_value_revision','absolute_pe_revision_control','matched_equal_weight','matched_momentum','absolute_equal_weight','absolute_momentum'],
        'code':{name:checksum(Path(__file__).parent/name) for name in ('run_value_limit_study.py','value_limit_replay.py',
            'limit_execution.py','value_plan.py','forecast_research_v3.py','strategy_research.py')},
        'historical_status':'reconstruction with explicit execution/data gaps; no production approval',
        'learning':'fixed source-based rules, no tuning on evaluated years; no learned return/hit probability',
        'position_limit':20,'entry_valid_sessions':3,'publication_assumption':'09:05 Beijing on execution day, previous-close signals'}
    path=out/'PROTOCOL.json'
    if path.exists() and json.loads(path.read_text())!=spec:raise ValueError('frozen protocol changed')
    if (out/'COMPLETE.json').exists():raise ValueError('completed run is immutable')
    dump(path,spec);code=out/'code-at-execution';code.mkdir(exist_ok=True)
    for name in spec['code']:shutil.copy2(Path(__file__).parent/name,code/name)
    with gzip.open(a.earnings_source,'rt') as f:source=[json.loads(line) for line in f]
    data=load_dataset(a.dataset,'all');features=prepare_features(data)
    variants,audit=signals(data,features,source,cfg);dump(out/'INPUT_COVERAGE.json',audit)
    print(json.dumps({'phase':'signals','coverage':audit}),flush=True)
    all_results={};summaries=[];annual=[]
    for cohort in spec['cohorts']:
        allowed=np.ones(len(data['symbols']),bool) if cohort=='all' else data['cohort']==cohort
        for horizon in spec['horizons']:
            for cost in spec['cost_bps_per_side']:
                for variant,(score,limits) in variants.items():
                    name=f'{cohort}-h{horizon}-c{cost}-{variant}'
                    target=out/(name+'.json.gz')
                    receipt_path=out/(name+'.receipt.json')
                    if target.exists():
                        receipt=json.loads(receipt_path.read_text())
                        if receipt['sha256']!=checksum(target) or receipt['protocol_sha256']!=checksum(path):
                            raise ValueError('cashbook checkpoint changed')
                        with gzip.open(target,'rt') as f:r=json.load(f)
                    else:
                        r=replay(data,features,np.where(allowed[None,:],score,np.nan),limits,start,end,
                            horizon=horizon,fee_bps=cost,initial_cash=cfg['initial_paper_cash'],size=cfg['max_candidates'],expiry=cfg['entry_valid_sessions'])
                        temp=target.with_suffix('.tmp')
                        with gzip.open(temp,'wt') as f:json.dump(r,f,ensure_ascii=False,allow_nan=False)
                        temp.replace(target)
                        dump(receipt_path,{'sha256':checksum(target),'protocol_sha256':checksum(path)})
                    # Each persisted cashbook is hashed for audit/resume.
                    all_results[(cohort,horizon,cost,variant)]=r
                    print(json.dumps({'phase':'replay','name':name,'orders':r['metrics']['order_count'],'cagr':r['metrics']['cagr']}),flush=True)
    for (cohort,horizon,cost,variant),r in all_results.items():
        baseline=all_results[cohort,horizon,cost,'absolute_equal_weight' if variant.startswith('absolute_') else 'matched_equal_weight']['metrics']
        verdict=qualify(r['metrics'],baseline,r['quality'],0,cfg)
        r['validation']=verdict
        row={'cohort':cohort,'horizon':horizon,'cost':cost,'model':variant,
            **{k:r['metrics'][k] for k in ('cagr','maximum_drawdown','period_win_rate','order_count','fill_rate','closed_trades','terminal_stale_value')},
            'baseline_cagr_advantage':r['metrics']['cagr']-baseline['cagr'],
            'balanced_passed':verdict['passed'],'failed_checks':','.join(verdict['failed'])}
        summaries.append(row)
        for year,values in r['metrics']['annual'].items():annual.append({'cohort':cohort,'horizon':horizon,'cost':cost,'model':variant,'year':year,**values})
    for name,rows in [('COSTED_RESULTS.csv',summaries),('ANNUAL_RESULTS.csv',annual)]:
        with (out/name).open('w') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    receipt={'status':'complete','runs':len(summaries),'rows':summaries,'approved':False,
        'cashbooks':{p.name:checksum(p) for p in out.glob('*.json.gz')},'protocol_sha256':checksum(path)}
    dump(out/'COMPLETE.json',receipt)
    return 0

if __name__=='__main__':raise SystemExit(main())
