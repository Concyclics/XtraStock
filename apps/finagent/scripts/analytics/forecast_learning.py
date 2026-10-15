"""Small, auditable stock research learner. Never a production approval path.

History is reconstructed from today's stored prices, not historical system knowledge.
The evaluated candidate and the refreshed forward candidate are separate artifacts.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import Counter
from typing import Any

import numpy as np

VERSION = 'stock-learning-reconstruction-v3'
CONTRACT = 'stock-learning-input-v1'
FEATURES = ('return_5d', 'return_20d', 'volatility_20d', 'drawdown_20d', 'volume_ratio_20d')
DEFAULT_CODES = ('300750','600000','600016','600030','600036','600050','601166','601398','601857')
LIMITATIONS = [
    '历史样本按当前保存的价格重构，不代表系统在历史日期已知这些数据。',
    '研究允许单一来源行情；复权必须为已验证状态，连续窗口内缺失或冲突数据不填充。',
    '固定个股研究样本存在存续与选择偏差，不能外推为全市场选股收益。',
    '历史部分交易日历由已有行情重构；前瞻预测只使用已验证日历。',
    '收益是下一交易日开盘至第N个交易日收盘的复权价格变化，不代表成交或实盘收益。',
    '刷新模型尚需独立前瞻验证；没有生产批准，不提供保证收益或必达目标价。',
]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def price_content_digest(rows):
    # Same-value refresh timestamps do not change the learned sample; changed
    # prices, quality, adjustment or source version do. Actual cutoff is retained
    # separately in every frozen prediction and model's input statistics.
    return digest([{k:v for k,v in r.items() if k not in ('source_available_at','source_updated_at')}
                   for r in rows])


def usable(row):
    return (row.get('quality_status') in ('verified', 'unverified')
            and row.get('adjustment_quality') == 'verified'
            and all(row.get(k) is not None and np.isfinite(float(row[k])) and float(row[k]) > 0
                    for k in ('open','close','volume'))
            and row.get('source_available_at') is not None)


def can_record_forward(signal_date, latest_completed_session, entry_at, now):
    """The real next open, not the local calendar date, closes the recording window."""
    return (signal_date == latest_completed_session and entry_at is not None
            and entry_at.tzinfo is not None and now.tzinfo is not None and now < entry_at)


def build_examples(prices, calendar, horizon, as_of, cutoff):
    """Exact calendar windows; no stretch over missing sessions or delayed sources."""
    dates = [r['trade_date'] for r in calendar]
    calendar_map = {r['trade_date']:r for r in calendar}
    by_code = {}
    for p in prices:
        by_code.setdefault(p['stock_code'], {})[p['dt']] = p
    examples, current, issues = [], [], {}
    for code, series in by_code.items():
        counts = Counter()
        for pos, day in enumerate(dates):
            if day > as_of or pos < 20:
                continue
            window_days = dates[pos-20:pos+1]
            window = [series.get(d) for d in window_days]
            failures = []
            if any(p is None for p in window):
                failures.append('missing_price_sessions')
            elif not all(usable(p) for p in window):
                failures.append('unusable_price_or_adjustment')
            elif any(p['source_available_at'] > cutoff for p in window):
                failures.append('input_not_known_at_cutoff')
            if failures:
                counts.update(failures)
                if day == as_of:
                    issues[code] = {'status':'blocked','reasons':failures,
                        'missing_dates':[str(d) for d,p in zip(window_days,window) if p is None]}
                continue
            close = np.array([float(p['close']) for p in window])
            volumes = np.array([float(p['volume']) for p in window])
            x = [float(close[-1]/close[-6]-1), float(close[-1]/close[0]-1),
                 float(np.std(close[1:]/close[:-1]-1, ddof=1)),
                 float(close[-1]/max(close[-20:])-1), float(volumes[-1]/np.mean(volumes[:-1]))]
            feature = {'stock_code':code,'name':window[-1]['name'],'signal_date':str(day), 'x':x,
                       'reference_close':float(window[-1]['raw_close']),
                       'feature_available_at':str(max(p['source_available_at'] for p in window)),
                       'feature_source_digest':price_content_digest(window),
                       'bar_quality_counts':dict(Counter(p['quality_status'] for p in window)),
                       'historical_calendar_verified':all(calendar_map[d]['is_verified'] for d in window_days)}
            future_days = dates[pos+1:pos+horizon+1]
            if day == as_of:
                # No backdated forward predictions. Caller also checks actual current date.
                forward_ok = (len(future_days) == horizon and calendar_map[future_days[0]].get('open_at') is not None
                              and all(calendar_map[d]['is_verified'] for d in future_days)
                              and all(calendar_map[d]['is_verified'] for d in window_days))
                if forward_ok:
                    current.append({**feature,'entry_date':str(future_days[0]),'exit_date':str(future_days[-1]),
                                    'entry_at':str(calendar_map[future_days[0]]['open_at']),
                                    'horizon':horizon,'source_window':window})
                    issues[code] = {'status':'ready','reasons':[]}
                else:
                    issues[code] = {'status':'blocked','reasons':['forward_or_feature_calendar_not_verified']}
            if len(future_days) != horizon or future_days[-1] > as_of:
                continue
            future = [series.get(d) for d in future_days]
            if any(p is None for p in future):
                counts['missing_label_sessions'] += 1
                continue
            if not all(usable(p) and p['source_available_at'] <= cutoff for p in future):
                counts['unusable_or_unavailable_label'] += 1
                continue
            close_time = calendar_map[future_days[-1]].get('close_at')
            if close_time is None or close_time > cutoff:
                counts['label_not_mature'] += 1
                continue
            examples.append({**feature,'entry_date':str(future_days[0]),'exit_date':str(future_days[-1]),
                             'horizon':horizon, 'y':float(future[-1]['close']/future[0]['open']-1),
                             'label_available_at':str(max(p['source_available_at'] for p in future)),
                             'label_source_digest':price_content_digest(future)})
        issues.setdefault(code, {'status':'blocked','reasons':['no_as_of_feature']})
        issues[code]['excluded_historical_windows'] = dict(counts)
    return sorted(examples,key=lambda e:(e['signal_date'],e['stock_code'])), current, issues


def split_four_ways(rows, tune_days=63, calibration_days=63, test_days=126, min_train_days=252):
    """Date-grouped train/tune/calibration/final test; purge using actual exit dates."""
    dates = sorted({r['signal_date'] for r in rows})
    if len(dates) < min_train_days+tune_days+calibration_days+test_days:
        raise ValueError(f'insufficient mature dates: {len(dates)}; need at least {min_train_days+tune_days+calibration_days+test_days}')
    test_start = dates[-test_days]
    cal_start = dates[-test_days-calibration_days]
    tune_start = dates[-test_days-calibration_days-tune_days]
    pieces = {
        'train':[r for r in rows if r['signal_date'] < tune_start and r['exit_date'] < tune_start],
        'tune':[r for r in rows if tune_start <= r['signal_date'] < cal_start and r['exit_date'] < cal_start],
        'calibration':[r for r in rows if cal_start <= r['signal_date'] < test_start and r['exit_date'] < test_start],
        'test':[r for r in rows if r['signal_date'] >= test_start],
    }
    minimums = {'train':min_train_days,'tune':25,'calibration':25,'test':60}
    for name, part in pieces.items():
        n = len({r['signal_date'] for r in part})
        if n < minimums[name]:
            raise ValueError(f'insufficient independent date groups in {name}: {n}')
    return pieces


def fit(rows, alpha):
    x=np.asarray([r['x'] for r in rows],float); y=np.asarray([r['y'] for r in rows],float)
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError('non-finite training inputs')
    mean=x.mean(axis=0); std=x.std(axis=0); std=np.where(std>1e-8,std,1)
    z=np.column_stack([np.ones(len(x)),np.clip((x-mean)/std,-8,8)])
    penalty=np.eye(z.shape[1])*alpha; penalty[0,0]=0
    beta=np.linalg.solve(z.T@z+penalty,z.T@y)
    return {'mean':mean.tolist(),'std':std.tolist(),'beta':beta.tolist(),'alpha':alpha,'features':list(FEATURES)}


def predict(params, rows):
    if params.get('kind') == 'purged-robust-volatility-v3':
        try:
            from .forecast_validation import predict_candidate
        except ImportError:
            from forecast_validation import predict_candidate
        return predict_candidate(params, rows)
    x=np.asarray([r['x'] for r in rows],float)
    z=np.column_stack([np.ones(len(x)),np.clip((x-np.array(params['mean']))/np.array(params['std']),-8,8)])
    return z@np.array(params['beta'])


def prediction_intervals(params, rows, estimates=None):
    if params.get('kind') == 'purged-robust-volatility-v3':
        try:
            from .forecast_validation import intervals
        except ImportError:
            from forecast_validation import intervals
        return intervals(params, rows, estimates)
    pred = predict(params, rows) if estimates is None else np.asarray(estimates)
    lo, hi = params['residual_interval_80']
    return pred + lo, pred + hi


def baseline_momentum(rows, horizon):
    return np.asarray([r['x'][1]*horizon/20 for r in rows],float)


def metrics(rows,pred,horizon,interval=None):
    actual=np.asarray([r['y'] for r in rows],float)
    zero=np.zeros(len(rows)); momentum=baseline_momentum(rows,horizon)
    by_date={}
    # One aggregate comparator, never the hindsight per-observation oracle.
    comparator = zero if np.abs(actual-zero).mean() <= np.abs(actual-momentum).mean() else momentum
    for r,e in zip(rows,np.abs(actual-pred)-np.abs(actual-comparator)):
        by_date.setdefault(r['signal_date'],[]).append(float(e))
    out={'rows':len(rows),'dates':len(by_date),'start':min(r['signal_date'] for r in rows),
         'end':max(r['signal_date'] for r in rows),'outcomes_through':max(r['exit_date'] for r in rows),
         'mae':float(np.abs(actual-pred).mean()),'zero_return_mae':float(np.abs(actual).mean()),
         'momentum_mae':float(np.abs(actual-momentum).mean()),
         'rmse':float(np.sqrt(((actual-pred)**2).mean())),
         'direction_accuracy':float((np.sign(actual)==np.sign(pred)).mean()),
         'positive_return_rate':float((actual>0).mean()),
         'mean_daily_best_aggregate_baseline_mae_difference':float(np.mean([np.mean(x) for x in by_date.values()]))}
    if interval:
        lo,hi=interval
        out['coverage_80']=float(((actual>=pred+lo)&(actual<=pred+hi)).mean())
        out['interval_mean_width']=float(hi-lo)
    return out


def train_legacy_research(rows,horizon):
    pieces=split_four_ways(rows)
    trials=[]
    for alpha in (1.0,10.0,100.0,1000.0):
        model=fit(pieces['train'],alpha)
        score=metrics(pieces['tune'],predict(model,pieces['tune']),horizon)
        trials.append((score['mae'],alpha,model))
    _,alpha,evaluated=min(trials,key=lambda x:(x[0],x[1]))
    residual=np.asarray([r['y'] for r in pieces['calibration']])-predict(evaluated,pieces['calibration'])
    interval=np.quantile(residual,[.1,.9]).tolist()
    test=metrics(pieces['test'],predict(evaluated,pieces['test']),horizon,interval)
    passed=(test['mae'] < min(test['zero_return_mae'],test['momentum_mae'])
            and .75 <= test['coverage_80'] <= .90)
    # The refresh learns newer mature results. Its residual calibration is disjoint.
    dates=sorted({r['signal_date'] for r in rows}); cutoff=dates[-63]
    refresh_train=[r for r in rows if r['exit_date']<cutoff]
    refresh_cal=[r for r in rows if r['signal_date']>=cutoff]
    refreshed=fit(refresh_train,alpha)
    fresh_residual=np.asarray([r['y'] for r in refresh_cal])-predict(refreshed,refresh_cal)
    refreshed['residual_interval_80']=np.quantile(fresh_residual,[.1,.9]).tolist()
    refreshed['trained_signal_through']=max(r['signal_date'] for r in refresh_train)
    refreshed['trained_outcome_through']=max(r['exit_date'] for r in refresh_train)
    refreshed['calibration_signal_from']=cutoff
    refreshed['calibration_outcome_through']=max(r['exit_date'] for r in refresh_cal)
    boundaries={name:{'rows':len(part),'dates':len({r['signal_date'] for r in part}),
                      'signal_start':min(r['signal_date'] for r in part),'signal_end':max(r['signal_date'] for r in part),
                      'outcome_end':max(r['exit_date'] for r in part)} for name,part in pieces.items()}
    return {'parameters':refreshed, 'evaluation':{'final_test':test,'splits':boundaries,
            'selected_alpha':alpha,'tuning_trials':[{'alpha':a,'mae':m} for m,a,_ in trials],
            'evaluated_parameters':evaluated,'evaluated_residual_interval_80':interval,
            'research_validation_passed':passed,'production_approved':False,
            'evaluation_scope':'Frozen earlier candidate. Refreshed forward candidate needs prospective validation.',
            'validation_reuse_warning':'Successive refreshes reuse historical periods; they are not independent final tests.',
            'overlap_warning':'Multi-day outcomes overlap within each block; rows are not independent bets.',
            'cost_assumption_bps':20,'cost_assumption_note':'Illustrative deduction only; no fills, turnover or liquidity simulation.',
            'knowledge_mode':'public_reconstruction','limitations':LIMITATIONS}}


def train_research(rows, horizon):
    try:
        from .forecast_validation import train_validated
    except ImportError:
        from forecast_validation import train_validated
    result = train_validated(rows, horizon)
    result['evaluation']['limitations'] = LIMITATIONS
    return result


def settle_prediction(prediction, prices, calendar, cutoff):
    """Return a revision-ready observed result only after every holding session exists."""
    start=dt.date.fromisoformat(str(prediction['entry_date'])); end=dt.date.fromisoformat(str(prediction['exit_date']))
    days=[r for r in calendar if start<=r['trade_date']<=end]
    if len(days)!=prediction['horizon'] or not all(r['is_verified'] and r['close_at']<=cutoff for r in days):
        return None
    by_date={p['dt']:p for p in prices if p['stock_code']==prediction['stock_code']}
    selected=[by_date.get(r['trade_date']) for r in days]
    if any(p is None or not usable(p) or p['source_available_at']>cutoff for p in selected):
        return None
    actual=float(selected[-1]['close']/selected[0]['open']-1)
    estimate=prediction['expected_gross_return']; lo,hi=prediction['interval_80']
    return {'realized_gross_return':actual,'absolute_error':abs(actual-estimate),
            'zero_return_absolute_error':abs(actual),
            'momentum_absolute_error':abs(actual-prediction['momentum_baseline']),
            'interval_covered':lo<=actual<=hi,'source_digest':price_content_digest(selected),
            'source_available_at':str(max(p['source_available_at'] for p in selected)),
            'return_basis':'next_open_to_horizon_close_total_return_proxy','executable_result':False}
