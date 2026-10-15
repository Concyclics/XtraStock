#!/usr/bin/env python3
"""Explicit approval audit; never called by a timer or training pipeline."""
import argparse,json
from common import connect
from forecast_baseline import MODEL_VERSION

def main():
 p=argparse.ArgumentParser();p.add_argument('--model-id',required=True);p.add_argument('--approved-by',required=True)
 p.add_argument('--reason',required=True);p.add_argument('--approve',action='store_true');a=p.parse_args()
 with connect(autocommit=False) as c:
  row=c.execute("SELECT * FROM analytics.forecast_models WHERE model_id=%s FOR UPDATE",(a.model_id,)).fetchone()
  if not row:raise RuntimeError('unknown model')
  v=row['validation'];blockers=[]
  if row['model_version']!=MODEL_VERSION:blockers.append('superseded validation implementation')
  if v.get('input_contract_version')!='pit-input-v1' or v.get('strict_pit_ready') is not True:
   blockers.append('point-in-time input evidence is incomplete')
  if not v.get('passed'):blockers.append('return prediction or calibrated interval failed held-out gates')
  if len(v.get('folds',[]))<3:blockers.append('fewer than three held-out folds')
  if any(f['mae']>=f['zero_return_mae'] for f in v.get('folds',[])):blockers.append('a fold fails the zero-return baseline')
  print(json.dumps({'model_id':a.model_id,'blockers':blockers,'commit_requested':a.approve},ensure_ascii=False))
  if blockers:raise RuntimeError('approval blocked: '+'; '.join(blockers))
  if a.approve:
   c.execute("""UPDATE analytics.forecast_models SET status='approved',approved_by=%s,approved_at=clock_timestamp(),
    validation=validation||jsonb_build_object('approval_reason',%s::text) WHERE model_id=%s""",(a.approved_by,a.reason,a.model_id));c.commit()
  else:c.rollback()
 return 0
if __name__=='__main__':raise SystemExit(main())
