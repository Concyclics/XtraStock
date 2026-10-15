#!/usr/bin/env python3
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import numpy as np
from zoneinfo import ZoneInfo
from common import connect,jsonb
from factor_validation import purged_folds
from factor_input_contract import INPUT_CONTRACT_VERSION
from forecast_baseline import FEATURES,MODEL_VERSION,fit,predict,calibration


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--horizon',type=int,choices=(5,20,60),action='append')
    parser.add_argument('--sample-modulus',type=int,default=10);parser.add_argument('--round-trip-bps',type=float,default=50)
    args=parser.parse_args();cutoff=dt.datetime.now(ZoneInfo('Asia/Shanghai'))
    if args.sample_modulus<1 or args.round_trip_bps<0: parser.error('invalid budget/cost')
    with connect() as conn:
        conn.execute("SET statement_timeout='180s'")
        for horizon in args.horizon or (5,20):
            columns=','.join('f.'+field for field in FEATURES)
            assets=[r['asset_id'] for r in conn.execute("""SELECT asset_id FROM market.assets
                WHERE asset_type='stock' AND NOT (metadata ? 'identity_alias_of') AND mod(abs(hashtextextended(symbol,0)::numeric),%s)=0""",(args.sample_modulus,))]
            rows=conn.execute(f"""SELECT f.asset_id,f.dt,{columns},r.return_{horizon}d y,
                r.entry_date,r.entry_price,r.exit_date_{horizon}d exit_date,r.label_available_at_{horizon}d label_known_at
                FROM factor.feature_daily f JOIN factor.forward_returns r ON r.asset_id=f.asset_id AND r.signal_date=f.dt
                JOIN LATERAL (
                  SELECT v.* FROM market.asset_lifecycle_versions v WHERE v.asset_id=f.asset_id
                    AND v.valid_from<=f.dt AND (v.valid_to IS NULL OR v.valid_to>=f.dt)
                    AND v.available_at<=f.decision_cutoff
                  ORDER BY v.available_at DESC,v.valid_from DESC,v.lifecycle_version_id DESC LIMIT 1
                ) life ON life.lifecycle_status='active' AND life.is_tradable AND NOT life.is_st
                   AND NOT life.is_suspended AND life.quality_status='verified'
                WHERE f.dt>=%s::date-1500 AND f.asset_id=ANY(%s)
                  AND r.return_{horizon}d IS NOT NULL AND r.exit_date_{horizon}d<%s::date
                  AND f.input_contract_version='pit-input-v1' AND r.input_contract_version='pit-input-v1'
                  AND f.knowledge_mode='system' AND f.input_quality->>'strict_pit_ready'='true'
                  AND f.available_at<=f.decision_cutoff
                  AND f.decision_cutoff<=((f.dt+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
                  AND r.label_available_at_{horizon}d<%s
                  AND f.momentum_20d IS NOT NULL AND f.low_volatility_20d IS NOT NULL
                ORDER BY f.dt,f.asset_id""",(cutoff.date(),assets,cutoff.date(),cutoff)).fetchall()
            if not rows: raise RuntimeError(f'no mature strict-PIT labels for horizon {horizon}; rebuild contract inputs and verify lifecycle history before training')
            sessions=[r['trade_date'] for r in conn.execute("""SELECT trade_date FROM market.trading_calendar
                WHERE exchange='SSE' AND session_status='open' ORDER BY trade_date""")]
            session_index={d:i for i,d in enumerate(sessions)}
            prices=conn.execute("""SELECT b.asset_id,b.dt,b.volume,b.high,b.low,b.quality_status,
                b.high*coalesce(f.total_return_factor,1) total_return_high
                FROM market.canonical_bars_daily_v2 b LEFT JOIN market.adjustment_factors_daily f
                  ON f.asset_id=b.asset_id AND f.dt=b.dt
                WHERE b.asset_id=ANY(%s) AND b.dt>=%s::date-1500 AND b.dt<%s::date""",
                (assets,cutoff.date(),cutoff.date())).fetchall()
            price_map={(r['asset_id'],r['dt']):r for r in prices}
            executable=[]
            for row in rows:
                i=session_index.get(row['dt']);j=session_index.get(row['exit_date'])
                if i is None or j is None or j-i!=horizon or i+1>=len(sessions) or row['entry_date']!=sessions[i+1]: continue
                en=price_map.get((row['asset_id'],row['entry_date']));ex=price_map.get((row['asset_id'],row['exit_date']))
                if not en or not ex or any(not (b['volume'] and b['volume']>0 and b['high']>b['low'])
                    or b['quality_status'] in ('rejected','conflict','incomplete') for b in (en,ex)): continue
                # A +10% total-return first touch is a separate label from the terminal return.
                path=[price_map.get((row['asset_id'],d)) for d in sessions[i+1:j+1]]
                if any(b is None for b in path): continue
                hits=[k+1 for k,b in enumerate(path) if row['entry_price'] and b['total_return_high']>=row['entry_price']*1.1]
                row['first_hit']=min(hits) if hits else None
                executable.append(row)
            rows=executable
            print({'horizon':horizon,'executable_rows':len(rows)},flush=True)
            if not rows: raise RuntimeError(f'no mature strict-PIT labels for horizon {horizon}; rebuild contract inputs and verify lifecycle history before training')
            fingerprint=hashlib.sha256(json.dumps([MODEL_VERSION,horizon,args.round_trip_bps,rows],default=str,sort_keys=True).encode()).hexdigest()
            previous=conn.execute("SELECT model_id FROM analytics.forecast_models WHERE model_version=%s AND horizon_days=%s AND input_fingerprint=%s",(MODEL_VERSION,horizon,fingerprint)).fetchone()
            if previous: print({'reused_model':str(previous['model_id']),'horizon':horizon},flush=True);continue
            dates=np.array([r['dt'] for r in rows],dtype='datetime64[D]');exit_dates=np.array([r['exit_date'] for r in rows],dtype='datetime64[D]')
            label_known_dates=np.array([r['label_known_at'].astimezone(ZoneInfo('Asia/Shanghai')).date() for r in rows],dtype='datetime64[D]')
            x=np.array([[r[k] if r[k] is not None else np.nan for k in FEATURES] for r in rows],float);y=np.array([r['y'] for r in rows],float)
            folds=purged_folds([r['dt'] for r in rows],folds=3,test_sessions=80,gap=horizon,min_train_sessions=400)
            if len(folds)<3: raise RuntimeError(f'insufficient independent history for horizon {horizon}: {len(folds)} folds')
            hits=np.array([r['first_hit'] if r['first_hit'] is not None else np.nan for r in rows],float)
            oos=[];fold_results=[]
            for train_start,train_end,test_start,test_end in folds:
                train=(dates<=np.datetime64(train_end))&(exit_dates<np.datetime64(test_start))&(label_known_dates<np.datetime64(test_start));test=(dates>=np.datetime64(test_start))&(dates<=np.datetime64(test_end))
                if train.sum()<10000 or test.sum()<2000: raise RuntimeError('insufficient executable panel sample')
                fitted=fit(x[train],y[train]);pred=predict(fitted,x[test]);actual=y[test]
                fold_results.append({'train_end':str(train_end),'test_start':str(test_start),'test_end':str(test_end),
                    'train_rows':int(train.sum()),'test_rows':int(test.sum()),'mae':float(np.mean(abs(pred-actual))),
                    'zero_return_mae':float(np.mean(abs(actual)))})
                oos.append((pred,actual,dates[test],hits[test]))
            # Calibrate on earlier OOS blocks and measure coverage only on final untouched block.
            cal_p=np.concatenate([a[0] for a in oos[:-1]]);cal_y=np.concatenate([a[1] for a in oos[:-1]])
            cal_hits=np.concatenate([a[3] for a in oos[:-1]])
            buckets=calibration(cal_p,cal_y,first_hits=cal_hits)
            residual=cal_y-cal_p;lo,hi=np.quantile(residual,[.1,.9]);p,y_test,d,test_hits=oos[-1]
            hit_predictions=np.array([next((b['first_touch_probability'] for b in buckets if b['lo']<=v<=b['hi']),np.nan) for v in p])
            valid=np.isfinite(hit_predictions);hit_actual=np.isfinite(test_hits).astype(float)
            brier=float(np.mean((hit_predictions[valid]-hit_actual[valid])**2)) if valid.any() else None
            base_hit=float(np.isfinite(cal_hits).mean());base_brier=float(np.mean((base_hit-hit_actual)**2))
            selected_buckets=[next((b for b in buckets if b['lo']<=v<=b['hi']),None) for v in p]
            supported=np.array([b is not None for b in selected_buckets])
            lower=np.array([v+b['residual_q10'] if b else np.nan for v,b in zip(p,selected_buckets)])
            upper=np.array([v+b['residual_q90'] if b else np.nan for v,b in zip(p,selected_buckets)])
            coverage=float(np.mean((y_test[supported]>=lower[supported])&(y_test[supported]<=upper[supported]))) if supported.any() else 0.0
            support_coverage=float(supported.mean())
            mae=float(np.mean(abs(p-y_test)));baseline=float(np.mean(abs(y_test)))
            validation={'input_contract_version':INPUT_CONTRACT_VERSION,'strict_pit_ready':True,'passed':mae<baseline and .70<=coverage<=.90 and support_coverage>=.90 and all(f['mae']<f['zero_return_mae'] for f in fold_results),
                'calibration_support_coverage':support_coverage,'first_touch_brier':brier,'unconditional_hit_brier':base_brier,'hit_validation_passed':brier is not None and brier<=base_brier,
                'mae':mae,'zero_return_mae':baseline,'coverage_80':coverage,'test_dates':int(len(np.unique(d))),
                'folds':fold_results,'sample_modulus':args.sample_modulus,'cost_bps':args.round_trip_bps,
                'execution_constraints':'nonzero_volume_nonlocked_bars_exact_calendar_horizon',
                'limitations':['Daily bars do not prove fills','Source history is as currently reconstructed; no production approval implied',
                    'First-touch probability and conditional time use a fixed +10% total-return threshold; not a guaranteed price/date']}
            fitted=fit(x,y);fitted.update(calibration=buckets,round_trip_bps=args.round_trip_bps)
            model=conn.execute("""INSERT INTO analytics.forecast_models(model_version,horizon_days,trained_through,
                decision_cutoff,input_fingerprint,parameters,validation,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING model_id""",
                (MODEL_VERSION,horizon,max(r['exit_date'] for r in rows),cutoff,fingerprint,jsonb(fitted),jsonb(validation),'validated' if validation['passed'] else 'shadow')).fetchone()
            print(json.dumps({'model_id':str(model['model_id']),'horizon':horizon,'rows':len(rows),'validation':validation},ensure_ascii=False),flush=True)
    return 0
if __name__=='__main__':raise SystemExit(main())
