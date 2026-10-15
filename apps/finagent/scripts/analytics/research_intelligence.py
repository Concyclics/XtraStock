"""One bounded read contract for focus changes and observed model learning."""
from __future__ import annotations

import datetime as dt
from contextlib import nullcontext
from zoneinfo import ZoneInfo


def load_intelligence(conn, cutoff, as_of=None, stock_codes=None, limit=10, scope='all', experiment_project=None, experiment_filters=None, experiment_offset=0):
    if scope not in {'all', 'focus', 'learning','pool','experiments'}:
        raise ValueError('scope must be all, focus, learning, pool or experiments')
    if cutoff.tzinfo is None:
        raise ValueError('decision cutoff must include timezone')
    day = cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date()
    as_of = dt.date.fromisoformat(str(as_of)) if as_of else day
    if as_of > day:
        raise ValueError('as_of_date must not exceed the decision cutoff date')
    limit = max(1, min(20, int(limit)))
    codes = list(dict.fromkeys(stock_codes or []))[:20] or None
    focus = learning = pool = None
    component_errors=[]
    def isolated(name, loader):
        # A PostgreSQL error aborts the transaction until the savepoint rolls
        # back. Catch outside the context so the next source remains readable.
        try:
            transaction=conn.transaction() if conn is not None else nullcontext()
            with transaction:
                return loader(conn,cutoff,as_of,codes,limit)
        except Exception as exc:
            component_errors.append({'component':name,'error_type':type(exc).__name__,
                'message':'该来源暂时读取失败，保留其他可用研究结果。'})
            return {'status':'unavailable','update_status':'unavailable','rows':[],
                    'analysis_available':False,'reason':'component_read_failed'}
    if scope in {'all','focus'}:
        from scripts.analytics.research_focus import load_focus
        focus=isolated('focus',load_focus)
    if scope in {'all','learning'}:
        from scripts.analytics.learning_state import load_learning_state
        learning=isolated('learning',load_learning_state)
    if scope in {'all','focus','pool'} and conn is not None:
        from scripts.analytics.candidate_pool import load_pool
        pool=isolated('candidate_pool',load_pool)
    experiments=None
    if scope=='experiments' and conn is not None:
        from scripts.analytics.research_asset_registry import load_registry_summary,load_experiment_results
        # Each citable row carries units and yearly method notes. Small pages
        # prevent tool-budget compaction from skipping evidence between offsets.
        limit=min(limit,5)
        result_rows=load_experiment_results(conn,cutoff,experiment_project,limit,experiment_filters,experiment_offset)
        experiments={'catalog':load_registry_summary(conn,cutoff,limit,experiment_project),'rows':result_rows,
                     'page_size':limit,
                     'offset':int(experiment_offset),
                     'next_offset':int(experiment_offset)+len(result_rows) if len(result_rows)==limit else None}
    return {
        'experiments':experiments,
        'as_of_date': as_of, 'decision_cutoff': cutoff, 'scope': scope,
        'focus': focus, 'learning': learning,'candidate_pool':pool,
        'component_errors':component_errors,
        'usage': '研究关注解释证据变化；学习结果说明已观察表现，研究预测不自动成为买卖建议。',
    }



def compact_monitor(monitor):
    """Keep monitoring conclusions within chat's budget; full ledger remains local."""
    if not isinstance(monitor,dict):return monitor
    result={k:v for k,v in monitor.items() if k not in {'by_stock','by_horizon','metric_units'}}
    group_keys={'horizon_trading_days','status','status_text','forecast_count','mature_observations',
        'signal_dates','independent_windows','invalid_observed_results','missing_due_results',
        'review_required','review_reasons','sufficient_for_research_monitoring','changes','comparison_pending_reason'}
    metric_keys={'observations','signal_dates','independent_windows','signal_start','signal_end',
        'mae','zero_return_mae','momentum_mae','coverage_80','interval_mean_width','sufficient'}
    result['by_horizon']=[]
    for row in monitor.get('by_horizon',[]):
        group={k:v for k,v in row.items() if k in group_keys}
        for name in ('recent','prior'):
            group[name]={k:v for k,v in (row.get(name) or {}).items() if k in metric_keys}
        result['by_horizon'].append(group)
    groups=list(monitor.get('by_stock',[]))
    groups.sort(key=lambda r:(not r.get('review_required'),-int(r.get('mature_observations') or 0),
                              str(r.get('stock_code')),r.get('horizon_trading_days',0)))
    counts={}
    for row in groups:counts[row.get('status','unknown')]=counts.get(row.get('status','unknown'),0)+1
    result['by_stock_status_counts']=counts
    result['by_stock_details_omitted']=max(0,len(groups)-8)
    result['by_stock']=[{k:v for k,v in row.items() if k in {'stock_code','horizon_trading_days',
        'status','mature_observations','signal_dates','independent_windows','review_required','review_reasons'}}
        for row in groups[:8]]
    result['detail_policy']='逐股窗口详情留在本机监测记录；本工具最多展示8个股票/期限组，优先需复核组，并保留全部状态计数、查询范围及整体窗口指标。其他组请指定股票再查；省略不代表无样本。单位见外层metric_units。'
    return result


def intelligence_rows(state):
    """Keep compact source-bound records instead of returning uncitable prose."""
    rows=list((state.get('experiments') or {}).get('rows',[]))
    pool=state.get('candidate_pool') or {}
    if pool.get('status')=='available':
        mining=pool.get('mining') or {}
        rows.append({'component':'candidate_pool_summary','as_of_date':max(pool.get('as_of_date') or '',mining.get('as_of_date') or ''),
            'available_at':max((dt.datetime.fromisoformat(str(value).replace('Z','+00:00')) for value in
                (pool.get('decision_cutoff'),mining.get('updated_at'),pool.get('forecast_validation_updated_at')) if value),default=None),
            'state_counts':pool.get('summary',{}).get('state_counts'),
            'new_candidates':pool.get('summary',{}).get('new_candidates'),
            'state_changes':pool.get('summary',{}).get('state_changes'),
            'cadence':'每日发现和评分；每周首轮重排；每月首轮复核长期论点',
            'policy_scope':'保留期与替换预算指研究名单。重点层和观察层共用每周替换预算；基础数据失效退出与空缺补充单独记录。',
            'pool_policy':{k:v for k,v in pool.get('summary',{}).get('config',{}).items() if k in
                {'exploration_capacity','watch_capacity','core_capacity','weekly_replacements','replacement_margin',
                 'industry_core_limit','minimum_hold_days','cooling_days'}},
            'mining':pool.get('mining'),'forecast_validation':pool.get('forecast_validation')})
    for row in pool.get('rows',[]):
        rows.append({'component':'candidate_pool',**row,'as_of_date':row.get('as_of_date') or pool.get('as_of_date'),
            'available_at':row.get('available_at') or pool.get('decision_cutoff')})
    focus=state.get('focus') or {}
    for row in focus.get('rows',[]):
        rows.append({'component':'research_focus', **{k:v for k,v in row.items() if k!='references'},
            'as_of_date':focus.get('as_of_date') or row.get('as_of_date'),
            'available_at':focus.get('decision_cutoff') or row.get('updated_at')})
    learning=state.get('learning') or {}
    if learning and learning.get('reason')!='component_read_failed':
        summary={k:v for k,v in learning.items() if k not in {'references','scenarios','research_scenarios','rows'}}
        if 'prospective_monitor' in summary:summary['prospective_monitor']=compact_monitor(summary['prospective_monitor'])
        rows.append({'component':'learning_state', **summary})
        for row in learning.get('research_scenarios', learning.get('scenarios',learning.get('rows',[]))):
            rows.append({'component':'research_scenario', **{k:v for k,v in row.items() if k!='references'}})
    return rows
