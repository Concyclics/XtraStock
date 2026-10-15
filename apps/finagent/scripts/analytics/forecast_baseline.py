"""CPU ridge baseline with date-grouped, purged OOS and held-out calibration.

This is a research model until explicit governance approval. Price equivalents
assume the next entry price equals the reference close, and include total return.
"""
from __future__ import annotations
import numpy as np
import datetime as dt

FEATURES=('momentum_20d','reversal_5d','low_volatility_20d','research_breadth_20d')
MODEL_VERSION='ridge-total-return-pit-v4'

def fit(x,y,alpha=100.0):
    x=np.asarray(x,float);y=np.asarray(y,float)
    mean=np.nanmean(x,axis=0);mean=np.where(np.isfinite(mean),mean,0)
    std=np.nanstd(x,axis=0);std=np.where(np.isfinite(std)&(std>1e-8),std,1)
    z=np.clip((np.where(np.isfinite(x),x,mean)-mean)/std,-8,8)
    z=np.column_stack([np.ones(len(z)),z]);penalty=np.eye(z.shape[1])*alpha;penalty[0,0]=0
    beta=np.linalg.solve(z.T@z+penalty,z.T@y)
    return {'mean':mean.tolist(),'std':std.tolist(),'beta':beta.tolist(),'alpha':alpha,'features':list(FEATURES)}

def predict(model,x):
    x=np.asarray(x,float);mean=np.array(model['mean']);std=np.array(model['std'])
    z=np.clip((np.where(np.isfinite(x),x,mean)-mean)/std,-8,8)
    return np.column_stack([np.ones(len(z)),z])@np.array(model['beta'])

def calibration(predicted,actual,bins=5,first_hits=None):
    predicted=np.asarray(predicted);actual=np.asarray(actual);out=[]
    boundaries=np.unique(np.quantile(predicted,np.linspace(0,1,bins+1)))
    for i in range(len(boundaries)-1):
        mask=(predicted>=boundaries[i]) & ((predicted<=boundaries[i+1]) if i==len(boundaries)-2 else (predicted<boundaries[i+1]))
        if mask.sum()<100: continue
        residual=actual[mask]-predicted[mask]
        out.append({'lo':float(boundaries[i]),'hi':float(boundaries[i+1]),'n':int(mask.sum()),
            'residual_q10':float(np.quantile(residual,.1)),'residual_q90':float(np.quantile(residual,.9)),
            'mean_return':float(actual[mask].mean()),'endpoint_above_10pct_rate':float((actual[mask]>=.1).mean())})
        if first_hits is not None:
            hits=np.asarray(first_hits)[mask];finite=hits[np.isfinite(hits)]
            out[-1].update(first_touch_probability=float(np.isfinite(hits).mean()),
                conditional_touch_sessions_q10_q50_q90=np.quantile(finite,[.1,.5,.9]).tolist() if len(finite)>=30 else None)
    return out

def scenario(model,x,reference_price,approved_factors=False,as_of=None):
    params=model['parameters'];validation=model['validation']
    value=float(predict(params,[x])[0]);buckets=params.get('calibration',[])
    bucket=next((b for b in buckets if b['lo']<=value<=b['hi']),None)
    valid_features=sum(np.isfinite(np.asarray(x,float)))>=3
    current_version=model.get('model_version')==MODEL_VERSION
    input_ready=validation.get('input_contract_version')=='pit-input-v1' and validation.get('strict_pit_ready') is True
    model_cutoff=model.get('decision_cutoff')
    fresh=as_of is None or (model_cutoff is not None and -1<=(as_of-dt.date.fromisoformat(str(model_cutoff)[:10])).days<=14)
    trained_through=model.get('trained_through')
    recent_labels=as_of is None or (trained_through is not None and 0<=(as_of-dt.date.fromisoformat(str(trained_through)[:10])).days<=21)
    eligible=(valid_features and current_version and input_ready and fresh and recent_labels and reference_price>0 and model.get('status')=='approved' and bool(model.get('approved_by')) and bool(model.get('approved_at'))
              and validation.get('passed') is True and approved_factors and bucket is not None)
    blockers=[]
    if not valid_features: blockers.append('当日有效特征不足')
    if not input_ready: blockers.append('模型缺少经过验证的输入时点与数据质量记录')
    if not current_version: blockers.append('模型版本已过期，须按当前流程重新验证')
    if not fresh: blockers.append('模型训练资料超过14天未更新或截止日期无效')
    if not recent_labels: blockers.append('最新成熟训练标签不足以覆盖近期行情')
    if not validation.get('passed'): blockers.append('样本外误差或校准未通过')
    if model.get('status')!='approved': blockers.append('模型尚未获人工批准')
    if not approved_factors: blockers.append('缺少通过完整资格审查的生产因子')
    if not bucket: blockers.append('预测值超出留出校准样本范围')
    result={'status':'available' if eligible else 'research_only','horizon_trading_days':model['horizon_days'],
        'model_version':model['model_version'],'prediction_eligible':eligible,'blockers':blockers,
        'reference_price':reference_price,'currency':'CNY','reference_assumption':'下一交易日实际买入价未知；价格等值暂以参考收盘价计算',
        'price_basis':'total-return equivalent, not a raw-price promise','expected_net_return':None,
        'price_equivalent_interval_80':None,'endpoint_above_10pct_probability':None,
        'first_touch_probability':None,'first_touch_time':None,
        'time_limit':'预测窗口为交易日区间；触达时间仅在命中条件下估计，不提供必达日期',
        'validation':{k:validation.get(k) for k in ('mae','zero_return_mae','coverage_80','test_dates','folds')}}
    if eligible:
        result.update(expected_net_return=value-params['round_trip_bps']/10000,
            price_equivalent_interval_80=[max(0,reference_price*(1+value+bucket['residual_q10'])),max(0,reference_price*(1+value+bucket['residual_q90']))],
            endpoint_above_10pct_probability=bucket['endpoint_above_10pct_rate'])
    if eligible and validation.get('hit_validation_passed') and bucket:
        result.update(first_touch_probability=bucket.get('first_touch_probability'),
            first_touch_time=bucket.get('conditional_touch_sessions_q10_q50_q90'),target_total_return=.1,
            target_price_equivalent=reference_price*1.1)
    return result
