"""Finite strategy search. Development never opens validation/final outcomes."""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()

import argparse
import dataclasses
import gzip
import hashlib
import itertools
import json
import os
from pathlib import Path
import time

os.environ.setdefault('OPENBLAS_NUM_THREADS','1')
os.environ.setdefault('OMP_NUM_THREADS','1')
from strategy_dataset import dump, checksum
from strategy_research import (VERSION,FAMILIES,Policy,load_dataset,prepare_features,backtest,
                               paired_comparison,balanced_score,research_gate)


def run_development(dataset,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    configs=[Policy(f,h,k,r) for f,h,k,r in itertools.product(FAMILIES,(5,10,20),(20,40),('none','lagged_market_trend'))]
    dump(output/'trial-plan.json',{'version':VERSION,'stage':'development','start':'2021-01-01','end':'2024-12-31',
        'configurations':[dataclasses.asdict(p) for p in configs],
        'baseline':'same eligibility, equal-weight research index, same next-open and cost constraints, fractional shares',
        'selection':'balanced_score fixed before outcomes; no validation/final data loaded',
        'data_manifest_sha256':checksum(Path(dataset)/'manifest.json'),
        'code_sha256':checksum(Path(__file__).with_name('strategy_research.py'))})
    d=load_dataset(dataset,cohort='discovery',until='2024-12-31');f=prepare_features(d)
    dump(output/'input-audit.json',{'securities':len(d['symbols']),'sessions':len(d['dates']),
        'eligible_counts':{str(y):{'min':int(f['eligible'][[str(x).startswith(y) for x in d['dates']]].sum(axis=1).min()),
                                  'max':int(f['eligible'][[str(x).startswith(y) for x in d['dates']]].sum(axis=1).max())}
                           for y in sorted({str(x)[:4] for x in d['dates']})},'anomalies':f['anomalies']})
    baselines={};summaries=[]
    for h in (5,10,20):
        p=Policy('equal_weight',rebalance=h,size=len(d['symbols']),round_lots=False)
        baselines[h]=backtest(d,f,p,'2021-01-01','2024-12-31')
        dump(output/f'baseline-h{h}.json',baselines[h])
    for index,p in enumerate(configs):
        start=time.monotonic()
        try:
            r=backtest(d,f,p,'2021-01-01','2024-12-31')
            comp=paired_comparison(r,baselines[p.rebalance]);gate=research_gate(r,comp)
            summary={'name':p.name,'policy':dataclasses.asdict(p),'metrics':r['metrics'],'comparison':comp,
                     'balanced_score':balanced_score(r,comp),'research_gate':gate,'seconds':round(time.monotonic()-start,3)}
            with gzip.open(output/f'{p.name}.json.gz','wt') as stream:json.dump(r,stream,ensure_ascii=False,allow_nan=False)
        except Exception as exc:
            summary={'name':p.name,'policy':dataclasses.asdict(p),'failed':True,'error':str(exc),'error_type':type(exc).__name__}
        summaries.append(summary);dump(output/'all-trials.json',summaries)
        print(json.dumps({'completed':index+1,'total':len(configs),'name':p.name,
                          'failed':summary.get('failed',False),'seconds':round(time.monotonic()-start,2)}),flush=True)
    ordered=sorted((s for s in summaries if not s.get('failed')),key=lambda s:s['balanced_score'],reverse=True)
    dump(output/'ranked-trials.json',ordered)
    dump(output/'SUMMARY.json',{'status':'complete','trials':len(configs),'failed':sum(s.get('failed',False) for s in summaries),
         'passed_development_research_gate':sum(s['research_gate']['passed'] for s in ordered),
         'leaders':ordered[:12],'validation_opened':False,'final_test_opened':False,'production_approved':False})
    return ordered


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--dataset',required=True);ap.add_argument('--output',required=True)
    args=ap.parse_args();run_development(args.dataset,args.output)
