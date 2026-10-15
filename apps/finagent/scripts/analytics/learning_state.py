"""Read-only, user-facing view of research learning. No IDs or hashes in output."""
from __future__ import annotations
import datetime as dt
import re
try:
    from .forecast_learning import VERSION,LIMITATIONS
    from .learning_monitor import UNITS,readable_metrics,load_prospective_monitor
    from .forecast_evidence import validation_evidence
except ImportError:  # direct analytics script invocation
    from forecast_learning import VERSION,LIMITATIONS
    from learning_monitor import UNITS,readable_metrics,load_prospective_monitor
    from forecast_evidence import validation_evidence


def compact_update_failure(run):
    """Explain only allowlisted operational metadata, never the raw exception."""
    if not run or run.get('status') != 'failed':
        return None
    summary=run.get('summary') or {}
    phase=summary.get('failure_phase','unknown')
    if not isinstance(phase,str):phase='unknown'
    phases={'preflight':'输入与交易日检查','pending_outcomes':'查询待核对的成熟结果',
        'input_read':'读取历史输入','calendar':'读取交易日历',
        'transaction_setup':'建立一致读取快照','merge_inputs':'合并分批行情',
        'settle_outcomes':'登记已成熟结果','cycle_record':'保存本轮学习记录'}
    if phase in phases:
        phase_text=phases[phase]
    elif isinstance(phase,str) and re.fullmatch(r'price_batch_[1-8]',phase):
        phase_text=f'读取第{phase.rsplit("_",1)[-1]}批行情'
    elif isinstance(phase,str) and (match:=re.fullmatch(r'model_horizon_(5|20|60)_(prepare|training|write|predictions)',phase)):
        action={'prepare':'准备样本','training':'拟合研究模型','write':'保存研究模型','predictions':'冻结前瞻研究记录'}[match[2]]
        phase_text=f'{match[1]}个交易日期限：{action}'
    else:
        phase='unknown';phase_text='失败阶段尚未记录'
    error_type=summary.get('error_type')
    if not isinstance(error_type,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',error_type):
        error_type='UnknownError'
    progress=summary.get('committed_progress')
    has_progress=isinstance(progress,dict)
    progress=progress if has_progress else {}
    counts={}
    for key in ('model_revisions_added','models_reused','predictions_added','outcome_revisions_added'):
        value=progress.get(key)
        counts[key]=value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None
    partial=any((counts[k] or 0)>0 for k in ('model_revisions_added','predictions_added','outcome_revisions_added'))
    if not has_progress:
        partial=None
    return {'phase':phase,'phase_text':phase_text,'error_type':error_type,
        'partial_cycle_writes':partial,'committed_progress':counts,
        'progress_scope':'仅已确认提交的研究记录；未知数量不等于零',
        'status_text':('本轮中途失败，部分研究记录已保存；不能称为完整更新' if partial else
                       '本轮执行失败；展示的既有模型以其实际数据日期与更新时间为准'),
        'production_approved':False}


def load_learning_state(conn,cutoff,as_of=None,stock_codes=None,limit=10):
    if cutoff.tzinfo is None: raise ValueError('cutoff must include timezone')
    limit=max(1,min(int(limit),20))
    codes=list(dict.fromkeys(stock_codes))[:20] if stock_codes else None
    exists=conn.execute("SELECT to_regclass('analytics.learning_models_v1') AS t").fetchone()['t']
    if not exists:
        return {'update_status':'not_installed','research_models':[],'research_scenarios':[],
                'mature_feedback':{'observations':None},'production_approved':False,'metric_units':UNITS,
                'prospective_monitor':{'status':'unavailable','status_text':'真实前瞻记录表尚未部署，样本数量未知'}}
    if isinstance(as_of,str): as_of=dt.date.fromisoformat(as_of)
    as_of=as_of or cutoff.astimezone(dt.timezone(dt.timedelta(hours=8))).date()
    # Midnight/holidays do not invent a new signal session. Keep the latest
    # completed session's genuinely forward forecast visible until its next open.
    completed=conn.execute("""SELECT max(trade_date) AS day FROM market.trading_calendar
        WHERE exchange='SSE' AND is_verified AND session_status='open'
          AND trade_date<=%s AND close_at<=%s AND available_at<=%s""",
        (as_of,cutoff,cutoff)).fetchone()['day']
    as_of=completed or as_of
    params={'cutoff':cutoff,'as_of':as_of,'codes':codes,'limit':limit,'version':VERSION}
    run=conn.execute("""SELECT status,completed_at,summary FROM analytics.learning_cycles_v1
        WHERE completed_at<=%(cutoff)s AND as_of<=%(as_of)s ORDER BY completed_at DESC LIMIT 1""",params).fetchone()
    models=conn.execute("""SELECT DISTINCT ON(horizon) horizon,as_of,created_at,input_stats,parameters,evaluation
        FROM analytics.learning_models_v1 WHERE created_at<=%(cutoff)s AND as_of<=%(as_of)s
          AND model_version=%(version)s
        ORDER BY horizon,created_at DESC""",params).fetchall()
    forecasts=conn.execute("""SELECT DISTINCT ON(p.stock_code,p.horizon)
          p.stock_code,p.name,p.horizon,p.signal_date,p.entry_date,p.exit_date,p.created_at,
          p.expected_gross_return,p.interval_80,p.research_qualified,
          p.frozen_input->>'reference_close' AS reference_close,
          m.parameters->>'trained_outcome_through' AS training_outcomes_through
        FROM analytics.learning_predictions_v1 p JOIN analytics.learning_models_v1 m USING(model_id)
        WHERE p.created_at<=%(cutoff)s AND p.signal_date=%(as_of)s AND m.created_at<=%(cutoff)s
          AND m.model_version=%(version)s
          AND (%(codes)s::text[] IS NULL OR p.stock_code=ANY(%(codes)s))
        ORDER BY p.stock_code,p.horizon,p.created_at DESC LIMIT %(limit)s""",params).fetchall()
    research_models=[]
    for m in models:
        e=m['evaluation'];test=e['final_test']
        readable=readable_metrics(test)
        delta=(test['mae']-test['zero_return_mae'])*100
        research_models.append({'horizon_trading_days':m['horizon'],'as_of_market_date':str(m['as_of']),
            'updated_at':str(m['created_at']),
            'status':'research_validated' if e['research_validation_passed'] else 'research_not_validated',
            'status_text':'历史研究检验通过，仍需真实前瞻验证' if e['research_validation_passed'] else '历史研究检验未通过，不能作为可靠收益预测',
            'historical_mature_samples':m['input_stats']['rows'],'historical_signal_dates':m['input_stats']['dates'],
            'stock_count':m['input_stats']['stocks'],'training_mode':'历史公开行情重构研究',
            'covered_stock_codes':m['input_stats']['stock_codes'],
            'final_test':{**{k:test[k] for k in ('rows','dates','start','end','mae','zero_return_mae','momentum_mae','coverage_80')},
                          'metric_units':UNITS,'readable':readable},
            'validation_summary':f"{readable['mae_text']}，比零收益基线{'高' if delta>=0 else '低'} {abs(delta):.3f} 个百分点；{readable['coverage_80_text']}",
            'validation_evidence':validation_evidence(m),
            'latest_mature_outcome_date':m['input_stats']['outcomes_through'],
            'refreshed_training_outcomes_through':m['parameters']['trained_outcome_through'],
            'validation_scope':'历史留出结果评价早期冻结模型；更新模型须继续接受前瞻检验',
            'production_approved':False})
    scenarios=[]
    for p in forecasts:
        qualified=p['research_qualified']
        scenarios.append({'stock_code':p['stock_code'],'name':p['name'],
            'horizon_trading_days':p['horizon'],'signal_date':str(p['signal_date']),
            'entry_date':str(p['entry_date']),'exit_date':str(p['exit_date']),
            'status':'research_only' if qualified else 'diagnostic_only',
            'expected_gross_return':p['expected_gross_return'] if qualified else None,
            'return_interval_80':p['interval_80'] if qualified else None,
            'expected_gross_return_percent':p['expected_gross_return']*100 if qualified else None,
            'return_interval_80_percent':[x*100 for x in p['interval_80']] if qualified else None,
            'nominal_interval_coverage':.8,
            'interval_explanation':'收益数值原值为比例，可读数值为百分比；区间名义覆盖率80%，不是盈利或方向正确概率',
            'reason':'历史检验通过，当前更新模型仍需前瞻检验' if qualified else '未胜过简单基线或区间校准未达标；数值仅留作内部前瞻验证',
            'production_approved':False,'return_basis':'下一交易日开盘至指定交易日收盘的复权价格变化，并非成交收益'})
    missing=[]
    if run:
        for m in run['summary'].get('models',[]):
            for code,item in m.get('current_inputs',{}).items():
                if item['status']!='ready' and (codes is None or code in codes):
                    missing.append({'stock_code':code,'horizon_trading_days':m['horizon'],
                        'missing_price_dates':item.get('missing_dates',[]),
                        'reason':{'missing_price_sessions':'近期行情存在缺失交易日',
                                  'unusable_price_or_adjustment':'近期价格或复权质量尚未通过',
                                  'input_not_known_at_cutoff':'输入晚于本次决策时间',
                                  'forward_or_feature_calendar_not_verified':'前瞻或近期窗口交易日历未核实',
                                  'no_as_of_feature':'当日尚未形成完整连续价格特征'}.get(item.get('reasons',[''])[0],'输入尚待核实')})
    monitor=load_prospective_monitor(conn,cutoff,as_of,codes,limit=20)
    failure=compact_update_failure(run)
    gaps_status=('unknown_after_failure' if failure else
                 'assessed_in_last_cycle' if run and any('current_inputs' in m for m in run['summary'].get('models',[]))
                 else 'not_assessed')
    feedback=[]
    for group in monitor.get('by_horizon',[]):
        recent=group['recent']
        feedback.append({'horizon':group['horizon_trading_days'],'forecasts':group['forecast_count'],
            'mature_observations':group['mature_observations'],'mature_signal_dates':group['signal_dates'],
            'metric_observations':recent['observations'],'metric_signal_dates':recent['signal_dates'],
            'metrics_window':'近期按期限扩展的窗口；总成熟数仅代表本次有界查询范围',
            **{k:recent[k] for k in ('mae','zero_return_mae','momentum_mae','coverage_80')},
            'metric_units':UNITS,'readable':recent['readable']})
    return {'update_status':run['status'] if run else 'not_run','as_of_market_date':str(as_of),
            'update_status_text':({'run':'已根据新成熟数据或修订更新研究记录','idempotent':'输入未变，复用已有模型与记录',
                'insufficient_data':'成熟历史不足，尚不能训练','locked':'已有学习任务运行，当前未重复执行',
                'failed':'最近学习任务执行失败'}.get(run['status'],'最近学习记录已保存') if run else '尚未执行学习任务'),
            'last_updated_at':str(run['completed_at']) if run else None,
            'update_failure':failure,
            'research_models':research_models,'research_scenarios':scenarios,
            'mature_feedback':{'observations':monitor.get('mature_observations'),
                               'by_horizon':feedback,'coverage':monitor.get('coverage'),
                               'basis':'与前瞻监测共用严格校验及有界窗口；每日首次冻结、cutoff前最新合格成熟结果'},
            'metric_units':UNITS,'prospective_monitor':monitor,
            'input_gaps':missing[:limit],'input_gaps_status':gaps_status,
            'input_gaps_status_text':{'unknown_after_failure':'最近一轮失败，空缺口列表不能证明输入齐全',
                'assessed_in_last_cycle':'仅反映最近完成一轮所检查的股票与期限',
                'not_assessed':'尚无本轮完整输入检查结果'}[gaps_status],
            'production_approved':False,
            'analysis_available':True,'limitations':LIMITATIONS}
