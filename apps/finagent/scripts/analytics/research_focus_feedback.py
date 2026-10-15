"""Adapt actual forward-ledger diagnostics to existing research attention.

The shared monitor owns maturity, point-in-time and sample-size checks. This
adapter never creates a stock candidate, changes its rank, or approves a model.
"""
from __future__ import annotations
import re

CONTRACT='focus-forward-feedback-v1'
_PRIVATE={'checked_at','decision_cutoff','source_digest','input_digest','references','prediction_ids','outcome_ids'}

def feedback_subjects(previous):
    subjects=sorted((p for p in previous if p.get('state')!='archived' and p.get('entity_type')=='stock'),
        key=lambda p:({'active':0,'watching':1,'cooling':2}.get(p.get('state'),3),-float(p.get('priority') or 0),p['focus_key']))
    return list(dict.fromkeys(c for p in subjects for c in p.get('stock_codes',[]) if re.fullmatch(r'\d{6}',c)))

def semantic(value):
    """Do not let polling time or internal lineage create attention revisions."""
    if isinstance(value,dict):
        return {k:semantic(v) for k,v in value.items() if k not in _PRIVATE and not k.startswith('_') and not k.endswith('_id')}
    if isinstance(value,list):return [semantic(v) for v in value]
    if isinstance(value,str) and re.fullmatch(r'[0-9a-f]{64}',value):return None
    return value

def focus_feedback_context(monitor,requested_codes):
    coverage=monitor.get('coverage') or {}
    queried=set(coverage.get('queried_stock_codes') or [])
    unknown=monitor.get('status') in ('unavailable','failed','error','not_installed')
    result={}
    for code in dict.fromkeys(requested_codes):
        if unknown:
            result[code]={'contract':CONTRACT,'availability':'unavailable','mature_observations':None,
                          'review_required':False,'horizons':[],'next_checks':[]}
            continue
        if code not in queried:continue  # A bounded query's omissions are unknown.
        groups=[r for r in monitor.get('by_stock',[]) if str(r.get('stock_code'))==code]
        if not groups:continue
        keys={'horizon','horizon_trading_days','status','mature_observations','signal_dates','independent_windows',
              'forecast_count','recent','prior','changes','review_reasons','limitations','status_text',
              'comparison_pending_reason','aggregation'}
        horizons=[semantic({k:v for k,v in r.items() if k in keys}) for r in groups]
        horizons.sort(key=lambda r:int(r.get('horizon_trading_days') or r.get('horizon') or 0))
        count=sum(int(r.get('mature_observations') or 0) for r in horizons)
        reviews=[r for r in horizons if r.get('status')=='review_required'
                 and int(r.get('mature_observations') or 0)>=20
                 and int(r.get('signal_dates') or 0)>=20
                 and int(r.get('independent_windows') or 0)>=5]
        checks=list(dict.fromkeys('复核预测反馈：'+str(reason).rstrip('。')+'。' for r in reviews for reason in r.get('review_reasons',[])))
        result[code]={'contract':CONTRACT,'availability':'available','mature_observations':count,
            'review_required':bool(reviews),'horizons':horizons,'next_checks':checks,
            'usage':'仅真实冻结前瞻结果的研究复核；同日不同期限不是独立交易，不增加关注评分，不作为收益承诺。'}
    return result

def collect_focus_feedback(conn,cutoff,as_of,stock_codes):
    codes=list(dict.fromkeys(stock_codes))[:20]
    if not codes:return {},{'status':'not_requested','queried_stock_codes':[]}
    try:
        try:
            from .learning_monitor import load_prospective_monitor
        except ImportError:
            from learning_monitor import load_prospective_monitor
        # Roll back only this optional diagnostic if the DB raises an error.
        with conn.transaction():
            monitor=load_prospective_monitor(conn,cutoff,as_of,codes,limit=20)
    except Exception as exc:
        monitor={'status':'unavailable','error_type':type(exc).__name__,'coverage':{'queried_stock_codes':[]}}
    contexts=focus_feedback_context(monitor,codes)
    return contexts,{'contract':CONTRACT,'status':monitor.get('status'),'coverage':monitor.get('coverage',{}),
        'focus_subject_count':len(list(dict.fromkeys(stock_codes))),'focus_subjects_omitted':max(0,len(list(dict.fromkeys(stock_codes)))-len(codes)),
        'error_type':monitor.get('error_type'),'mature_observations':sum(v['mature_observations'] or 0 for v in contexts.values())
            if monitor.get('status')!='unavailable' else None,
        'review_tasks':sum(v['review_required'] for v in contexts.values())}
