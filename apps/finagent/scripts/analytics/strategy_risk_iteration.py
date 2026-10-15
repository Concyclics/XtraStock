"""Bounded risk-budget refinement, before validation is opened."""
import argparse,dataclasses,gzip,itertools,json
from pathlib import Path
from strategy_dataset import dump,checksum
from strategy_research import Policy,load_dataset,prepare_features,backtest,paired_comparison,balanced_score,research_gate,VERSION


def main():
 ap=argparse.ArgumentParser();ap.add_argument('--dataset',required=True);ap.add_argument('--output',required=True);args=ap.parse_args()
 p=Path(args.output);p.mkdir(exist_ok=False,parents=True)
 configs=[Policy(f,h,k,target_vol=v) for f,h,k,v in itertools.product(
     ('defensive_trend','low_volatility','momentum_low_vol'),(5,10,20),(20,40),(.12,.18))]
 dump(p/'trial-plan.json',{'stage':'development_risk_refinement','version':VERSION,'configurations':[dataclasses.asdict(x) for x in configs],
    'why':'three development-leading families, two conventional annual volatility budgets; no validation outcomes observed',
    'data_sha256':checksum(Path(args.dataset)/'manifest.json'),'code_sha256':checksum(Path(__file__).with_name('strategy_research.py')),
    'validation_selection':'at most two distinct families; development gate and positive stressed CAGR; fixed balanced score',
    'no_further_grid_after_validation':True})
 d=load_dataset(args.dataset,until='2024-12-31');f=prepare_features(d)
 bases={h:backtest(d,f,Policy('equal_weight',rebalance=h,size=len(d['symbols']),round_lots=False),'2021-01-01','2024-12-31') for h in (5,10,20)}
 out=[]
 for i,policy in enumerate(configs):
  r=backtest(d,f,policy,'2021-01-01','2024-12-31');c=paired_comparison(r,bases[policy.rebalance])
  summary={'name':policy.name,'policy':dataclasses.asdict(policy),'metrics':r['metrics'],'comparison':c,
    'balanced_score':balanced_score(r,c),'research_gate':research_gate(r,c)}
  with gzip.open(p/(policy.name+'.json.gz'),'wt') as stream:json.dump(r,stream,allow_nan=False)
  out.append(summary);dump(p/'all-trials.json',out)
  print(json.dumps({'completed':i+1,'total':len(configs),'name':policy.name}),flush=True)
 dump(p/'ranked-trials.json',sorted(out,key=lambda x:x['balanced_score'],reverse=True))
 dump(p/'SUMMARY.json',{'trials':len(out),'passed':sum(x['research_gate']['passed'] for x in out),'validation_opened':False})


if __name__=='__main__':main()
