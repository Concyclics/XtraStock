"""Bounded research monitoring from real, frozen prospective predictions only.

No historical holdout row can enter this monitor. Review thresholds are operational
research rules, not significance tests, trading permissions or automatic promotion.
"""
from __future__ import annotations
import datetime as dt
import math
from collections import defaultdict
try:
    from .forecast_learning import VERSION,CONTRACT
except ImportError:
    from forecast_learning import VERSION,CONTRACT

MAX_CODES=20
MAX_ROWS=25000
LOOKBACK_DAYS=1600
MIN_SIGNAL_DATES=20
MIN_INDEPENDENT_WINDOWS=5
UNITS={
    'mae':'return_ratio',
    'mae_explanation':'收益预测与实际收益的绝对差平均值；0.025 表示误差 2.5 个百分点，不是盈利 2.5%。',
    'mae_display':'percentage_points',
    'coverage_80':'fraction_of_realized_returns_inside_interval',
    'coverage_explanation':'名义覆盖率为80%；实测覆盖率表示真实结果落入所报区间的比例，不是盈利概率或方向准确率。',
    'nominal_interval_coverage':.8,
}
THRESHOLDS={
    'minimum_mature_signal_dates':MIN_SIGNAL_DATES,
    'minimum_nonoverlapping_holding_windows':MIN_INDEPENDENT_WINDOWS,
    'recent_window_signal_dates':'max(20, 5 × horizon_trading_days)',
    'mae_vs_zero_relative_margin':.10,
    'mae_vs_zero_absolute_margin_percentage_points':.5,
    'coverage_review_range_percent':[65,95],
    'mae_deterioration_relative_margin':.25,
    'mae_deterioration_absolute_margin_percentage_points':.5,
    'excess_mae_deterioration_margin_percentage_points':.25,
    'coverage_nominal_gap_deterioration_percentage_points':10,
    'interpretation':'研究复核规则；并非统计显著性检验或生产准入标准。',
}


def _finite(value):
    try:
        result=float(value)
        return result if math.isfinite(result) else None
    except (TypeError,ValueError):
        return None


def readable_metrics(metrics):
    """Keep machine ratios alongside unambiguous human-readable percentages."""
    out={}
    labels={'mae':'预测平均绝对误差','zero_return_mae':'零收益基线误差','momentum_mae':'简单动量基线误差'}
    for key,label in labels.items():
        value=_finite(metrics.get(key))
        out[key+'_percentage_points']=value*100 if value is not None else None
        out[key+'_text']=f'{label} {value*100:.3f} 个百分点' if value is not None else f'{label}尚不可计算'
    coverage=_finite(metrics.get('coverage_80'))
    out['coverage_80_percent']=coverage*100 if coverage is not None else None
    out['coverage_80_text']=(f'名义80%预测区间的实测覆盖率 {coverage*100:.2f}%（非盈利或方向概率）'
                            if coverage is not None else '名义80%预测区间的实测覆盖率尚不可计算')
    return out


def _day(value):
    return value if isinstance(value,dt.date) else dt.date.fromisoformat(str(value)[:10])


def _independent_windows(rows):
    intervals=sorted({(_day(r['entry_date']),_day(r['exit_date'])) for r in rows},key=lambda x:(x[1],x[0]))
    n=0;last=dt.date.min
    for start,end in intervals:
        if start>last:
            n+=1;last=end
    return n


def _valid_observation(row):
    """Recompute errors from frozen estimates and observed returns, not status flags."""
    result=row.get('result')
    if not isinstance(result,dict):return None
    if row.get('observation_valid_at_cutoff') is not True:return None
    if result.get('return_basis')!='next_open_to_horizon_close_total_return_proxy':return None
    actual=_finite(result.get('realized_gross_return'))
    estimate=_finite(row.get('expected_gross_return'));momentum=_finite(row.get('momentum_baseline'))
    interval=row.get('interval_80')
    if actual is None or estimate is None or momentum is None or not isinstance(interval,(list,tuple)) or len(interval)!=2:
        return None
    lo,hi=(_finite(x) for x in interval)
    if lo is None or hi is None or lo>hi:return None
    return {**row,'mae':abs(actual-estimate),'zero_return_mae':abs(actual),
            'momentum_mae':abs(actual-momentum),'coverage_80':float(lo<=actual<=hi),
            'interval_width':hi-lo}


def _summary(rows):
    days=defaultdict(list)
    for row in rows:days[_day(row['signal_date'])].append(row)
    result={'observations':len(rows),'signal_dates':len(days),
            'independent_windows':_independent_windows(rows),
            'signal_start':str(min(days)) if days else None,'signal_end':str(max(days)) if days else None,
            'mae':None,'zero_return_mae':None,'momentum_mae':None,'coverage_80':None,'interval_mean_width':None}
    for key in ('mae','zero_return_mae','momentum_mae','coverage_80','interval_width'):
        # Avoid allowing a large cross-section on one day to dominate comparisons.
        if days:
            daily=[sum(r[key] for r in group)/len(group) for group in days.values()]
            result['interval_mean_width' if key=='interval_width' else key]=sum(daily)/len(daily)
    result['readable']=readable_metrics(result)
    result['sufficient']=(len(days)>=MIN_SIGNAL_DATES and result['independent_windows']>=MIN_INDEPENDENT_WINDOWS)
    return result


def monitor_group(rows,horizon,stock_code=None,truncated=False):
    valid=[x for row in rows if (x:=_valid_observation(row)) is not None]
    dates=sorted({_day(r['signal_date']) for r in valid})
    window=max(MIN_SIGNAL_DATES,5*horizon)
    recent_start=dates[-window] if len(dates)>=window else (dates[0] if dates else None)
    recent_rows=[r for r in valid if _day(r['signal_date'])>=recent_start] if dates else []
    # Prior outcomes must have finished before the recent signal period starts.
    older=[r for r in valid if _day(r['exit_date'])<recent_start] if dates else []
    previous_dates=sorted({_day(r['signal_date']) for r in older})[-window:]
    previous_set=set(previous_dates)
    previous_rows=[r for r in older if _day(r['signal_date']) in previous_set]
    recent=_summary(recent_rows);prior=_summary(previous_rows)
    result={'stock_code':stock_code,'horizon_trading_days':horizon,'status':'pending',
            'status_text':'等待足够真实前瞻结果成熟','forecast_count':len(rows),
            'mature_observations':len(valid),'signal_dates':len(dates),'independent_windows':_independent_windows(valid),
            'invalid_observed_results':sum(r.get('result') is not None for r in rows)-len(valid),
            'missing_due_results':sum(bool(r.get('outcome_due')) and r.get('result') is None for r in rows),
            'recent':recent,'prior':prior,'changes':None,'review_reasons':[],
            'review_required':False,'production_approved':False,'sufficient_for_research_monitoring':False,
            'aggregation':'各信号日等权；同日股票等权。持有期可能重叠，独立窗口数另列，不把样本行数当独立试验。'}
    if truncated:
        result['status_text']='本次有界查询发生截断，不能据不完整窗口判断漂移'
        return result
    if not recent['sufficient']:
        if not rows: result['status_text']='尚无真实冻结预测；历史留出样本不计入前瞻监测'
        elif not valid: result['status_text']='已有冻结预测，但尚无合格且成熟的真实结果'
        else: result['status_text']=f"近期只有 {recent['signal_dates']} 个成熟信号日、{recent['independent_windows']} 个不重叠持有窗口；至少需要20日和5个窗口"
        return result
    result.update(status='monitoring',status_text='已有足够真实样本，继续监测；尚不构成生产批准',
                  sufficient_for_research_monitoring=True)
    reasons=[]
    if recent['mae']>recent['zero_return_mae']*1.10 and recent['mae']-recent['zero_return_mae']>=.005:
        reasons.append('近期平均绝对误差比零收益基线高超过10%，且绝对差至少0.5个百分点，需复核模型')
    if recent['coverage_80']<.65 or recent['coverage_80']>.95:
        reasons.append('名义80%区间的近期实测覆盖率超出65%–95%复核范围，需检查校准及区间宽度')
    if prior['sufficient']:
        mae_change=recent['mae']-prior['mae']
        excess_change=(recent['mae']-recent['zero_return_mae'])-(prior['mae']-prior['zero_return_mae'])
        coverage_change=recent['coverage_80']-prior['coverage_80']
        result['changes']={'mae_change_percentage_points':100*mae_change,
            'zero_return_mae_change_percentage_points':100*(recent['zero_return_mae']-prior['zero_return_mae']),
            'excess_mae_change_percentage_points':100*excess_change,
            'coverage_change_percentage_points':100*coverage_change,
            'comparison_scope':'先前结果已在近期信号窗口开始前成熟；两段窗口不共享持有期结果'}
        if recent['mae']>prior['mae']*1.25 and mae_change>=.005 and excess_change>=.0025:
            reasons.append('近期误差较前期恶化超过25%且至少0.5个百分点，相对零收益基线的超额误差也恶化至少0.25个百分点')
        if abs(recent['coverage_80']-.8)-abs(prior['coverage_80']-.8)>=.10:
            reasons.append('近期覆盖率相对名义80%的偏离比前期增大至少10个百分点，需复核区间漂移')
    else:
        result['comparison_pending_reason']='前期尚未积累20个成熟信号日和5个不重叠持有窗口，暂不比较前后漂移'
    if reasons:result.update(status='review_required',status_text='真实前瞻结果触发研究复核规则，尚非显著性结论',review_required=True,review_reasons=reasons)
    return result


MONITOR_SQL="""
WITH first_recorded AS (
 SELECT DISTINCT ON(p.stock_code,p.signal_date,p.horizon)
   p.prediction_id,p.stock_code,p.horizon,p.signal_date,p.entry_date,p.exit_date,p.created_at,
   p.expected_gross_return,p.interval_80,p.momentum_baseline
 FROM analytics.learning_predictions_v1 p JOIN analytics.learning_models_v1 m USING(model_id)
 WHERE p.knowledge_mode='system_forward' AND m.model_version=%(version)s
   AND p.created_at<=%(cutoff)s AND m.created_at<=p.created_at
   AND p.signal_date BETWEEN %(as_of)s::date-%(lookback)s AND %(as_of)s
   AND p.stock_code=ANY(%(codes)s) AND p.horizon=ANY(%(horizons)s)
   AND p.frozen_input->>'contract'=%(contract)s
   AND p.created_at<(p.frozen_input->>'entry_at')::timestamptz
   AND (p.frozen_input->>'input_knowledge_cutoff')::timestamptz<=p.created_at
   AND (p.frozen_input->>'feature_available_at')::timestamptz <=
       (p.frozen_input->>'input_knowledge_cutoff')::timestamptz
 ORDER BY p.stock_code,p.signal_date,p.horizon,p.created_at,p.prediction_id
), ranked AS (
 SELECT p.*,row_number() OVER(PARTITION BY stock_code,horizon ORDER BY signal_date DESC) AS rn
 FROM first_recorded p
)
SELECT p.*,o.observed_at,o.result,(p.exit_date<=%(as_of)s) AS outcome_due,
       (ex.is_verified AND ex.available_at<=%(cutoff)s AND ex.close_at<=o.observed_at
        AND (o.result->>'source_available_at')::timestamptz<=o.observed_at) AS observation_valid_at_cutoff
FROM ranked p
LEFT JOIN market.trading_calendar ex ON ex.exchange='SSE' AND ex.trade_date=p.exit_date
    AND ex.session_status='open'
LEFT JOIN LATERAL (SELECT observed_at,result FROM analytics.learning_outcomes_v1 o
    WHERE o.prediction_id=p.prediction_id AND o.observed_at<=%(cutoff)s
    ORDER BY o.observed_at DESC,o.outcome_id DESC LIMIT 1) o ON p.exit_date<=%(as_of)s
WHERE p.rn<=11*p.horizon+40
ORDER BY p.signal_date DESC,p.stock_code,p.horizon LIMIT %(row_limit)s
"""


def load_prospective_monitor(conn,cutoff,as_of=None,stock_codes=None,limit=20):
    """limit counts stocks, not horizon groups. DB errors deliberately propagate."""
    if cutoff.tzinfo is None:raise ValueError('cutoff must have a timezone')
    exists=conn.execute("SELECT to_regclass('analytics.learning_outcomes_v1') AS t").fetchone()['t']
    if not exists:
        return {'status':'unavailable','status_text':'真实前瞻记录表尚未部署，样本数量未知',
                'by_stock':[],'by_horizon':[],'mature_observations':None,'production_approved':False}
    as_of=_day(as_of) if as_of else cutoff.astimezone(dt.timezone(dt.timedelta(hours=8))).date()
    completed=conn.execute("""SELECT max(trade_date) AS day FROM market.trading_calendar
        WHERE exchange='SSE' AND is_verified AND session_status='open' AND trade_date<=%s
          AND close_at<=%s AND available_at<=%s""",(as_of,cutoff,cutoff)).fetchone()['day']
    as_of=completed or as_of
    configured=conn.execute("""SELECT DISTINCT ON(horizon) horizon,input_stats->'stock_codes' AS codes
        FROM analytics.learning_models_v1 WHERE model_version=%s AND created_at<=%s AND as_of<=%s
        ORDER BY horizon,created_at DESC""",(VERSION,cutoff,as_of)).fetchall()
    horizons=sorted({r['horizon'] for r in configured}) or [5,20]
    requested=list(dict.fromkeys(stock_codes)) if stock_codes is not None else sorted({code for r in configured for code in (r['codes'] or [])})
    bound=max(1,min(int(limit),MAX_CODES));codes=requested[:bound]
    rows=conn.execute(MONITOR_SQL,{'version':VERSION,'contract':CONTRACT,'cutoff':cutoff,'as_of':as_of,
        'lookback':LOOKBACK_DAYS,'codes':codes,'horizons':horizons,'row_limit':MAX_ROWS+1}).fetchall() if codes else []
    row_truncated=len(rows)>MAX_ROWS;rows=rows[:MAX_ROWS]
    groups=defaultdict(list);by_h=defaultdict(list)
    for r in rows:groups[(r['stock_code'],r['horizon'])].append(r);by_h[r['horizon']].append(r)
    by_stock=[monitor_group(groups[(code,h)],h,code,row_truncated) for code in codes for h in horizons]
    by_horizon=[monitor_group(by_h[h],h,truncated=row_truncated) for h in horizons]
    status=('review_required' if any(r['review_required'] for r in by_stock+by_horizon)
            else 'monitoring' if any(r['sufficient_for_research_monitoring'] for r in by_stock+by_horizon) else 'pending')
    return {'status':status,'status_text':{'pending':'等待足够真实前瞻结果；不足不告警、不放行',
        'monitoring':'真实样本已达到研究监测数量，尚不构成生产批准',
        'review_required':'真实前瞻结果触发研究复核；并非统计显著性结论'}[status],
        'as_of_market_date':str(as_of),'by_stock':by_stock,'by_horizon':by_horizon,
        'coverage':{'queried_stock_codes':codes,'requested_stock_count':len(requested),
                    'truncated':row_truncated or len(requested)>len(codes),'row_truncated':row_truncated,
                    'retained_rows':len(rows),'lookback_days':LOOKBACK_DAYS,'max_codes':MAX_CODES},
        'mature_observations':sum(r['mature_observations'] for r in by_horizon),
        'source':'每日首次真实冻结预测；仅取查询cutoff前已录入的最新成熟结果版本，不使用历史训练或留出样本',
        'thresholds':THRESHOLDS,'metric_units':UNITS,'production_approved':False,
        'limitations':['监测对象为复权价格收益代理，不是实盘收益。',
                       '前后比较中的市场环境、股票范围可能变化；研究复核规则不能证明模型漂移具有统计显著性。',
                       '每股票每期限仅保留最近11×期限+40个信号日，最多20只股票；未查询的股票不计为零样本。']}
