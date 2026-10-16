"""Evidence-first valuations and immutable daily recommendation plans.

Relative valuation scenarios are not calibrated price forecasts. A positive
scenario return cannot authorize a user-facing buy/sell recommendation.
"""
from __future__ import annotations
from collections import defaultdict
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from statistics import median
try:
    from .limit_execution import stamp, first_eligible_session
except ImportError:
    from limit_execution import stamp, first_eligible_session

VERSION='daily-value-plan-v1'

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str,allow_nan=False).encode()).hexdigest()

def load_config(path=None):
    path=Path(path) if path else Path(__file__).resolve().parents[2]/'config/daily_value_plan.json'
    cfg=json.loads(path.read_text())
    if cfg['version']!=VERSION or cfg['primary_years']!=5 or cfg['mode'] not in ('shadow','approved'):
        raise ValueError('unsupported plan configuration')
    if not 0<cfg['entry_discount']<1 or not 0<=cfg['no_chase_discount']<1:
        raise ValueError('invalid entry policy')
    if not 1<=cfg['max_candidates']<=50 or not 1<=cfg['entry_valid_sessions']<=20:
        raise ValueError('invalid portfolio/order bounds')
    return cfg

def num(v):
    if v is None:return None
    f=float(v)
    if not math.isfinite(f):raise ValueError('nonfinite fundamental value')
    return f

def balanced_consensus(observations, cutoff, fiscal_year, max_age=180, actions=()):
    """One latest report per institution/year; conflicts and split bases stay gaps.

    Zero and negative latest EPS invalidate a broker's positive older EPS. API
    observations are usable only after their fetch time. Revisions require two
    explicit dated reports; refreshing a rolling API is never an EPS revision.
    """
    cutoff=stamp(cutoff); grouped=defaultdict(list)
    for r in observations:
        if r['metric']!='eps' or int(r['forecast_year'])!=fiscal_year or not r.get('institution'):continue
        if r['unit']!='CNY/share' or r['source_basis'] not in ('explicit_dated_report_text','observed_api_current_year'):
            continue
        known=stamp(r['known_at']);issued=dt.date.fromisoformat(str(r['report_date'])[:10])
        if known>cutoff or not 0<=(cutoff.date()-issued).days<=max_age:continue
        if r['source_basis']=='observed_api_current_year' and stamp(r['observed_at'])>cutoff:continue
        if any(issued<dt.date.fromisoformat(str(a['ex_date'])[:10])<=cutoff.date() and a.get('changes_share_basis') for a in actions):continue
        broker=''.join(r['institution'].split())
        grouped[broker].append(r)
    selected=[];revisions=[];conflicts=[]
    for broker,rows in sorted(grouped.items()):
        # An updated source snapshot wins within the same report/date, but never
        # turns an old publication into a new research recommendation.
        latest_day=max(str(r['report_date'])[:10] for r in rows)
        current=[r for r in rows if str(r['report_date'])[:10]==latest_day]
        by_document=defaultdict(list)
        for r in current:by_document[str(r.get('document_id') or r.get('source_identifier'))].append(r)
        chosen=[]
        for doc,items in by_document.items():
            dated=[r for r in items if r['source_basis']=='explicit_dated_report_text']
            values=dated or [max(items,key=lambda r:stamp(r['known_at']))]
            if len({num(r['value']) for r in values})!=1:
                conflicts.append({'institution':broker,'report_date':latest_day,'reason':'conflicting_same_report_eps'});continue
            chosen.append(max(values,key=lambda r:stamp(r['known_at'])))
        if len({num(r['value']) for r in chosen})!=1 or not chosen:
            conflicts.append({'institution':broker,'report_date':latest_day,'reason':'conflicting_same_day_eps'});continue
        r=chosen[0];selected.append(r)
        prior=[x for x in rows if x['source_basis']=='explicit_dated_report_text' and str(x['report_date'])[:10]<latest_day
            and (dt.date.fromisoformat(latest_day)-dt.date.fromisoformat(str(x['report_date'])[:10])).days<=120]
        if r['source_basis']=='explicit_dated_report_text' and prior and num(r['value'])>0:
            previous_day=max(str(x['report_date'])[:10] for x in prior)
            previous=[x for x in prior if str(x['report_date'])[:10]==previous_day]
            pv={num(x['value']) for x in previous}
            if len(pv)==1 and next(iter(pv))>0:
                revisions.append({'institution':broker,'value':num(r['value'])/next(iter(pv))-1,
                    'from_document':str(previous[0]['document_id']),'to_document':str(r['document_id']),
                    'from_date':previous_day,'to_date':latest_day})
    values=[num(r['value']) for r in selected]
    return {'fiscal_year':fiscal_year,'eps_median':median(values) if values else None,
        'eps_low':min(values) if values else None,'eps_high':max(values) if values else None,
        'institutions':len(values),'positive_institutions':sum(v>0 for v in values),
        'revision_median':median(r['value'] for r in revisions) if revisions else None,
        'revision_institutions':len(revisions),'revisions':revisions,'conflicts':conflicts,
        'evidence':[dict(r,value=num(r['value'])) for r in selected]}

def valuations(rows, config):
    peers=defaultdict(list)
    for r in rows:
        eps=r['consensus']['eps_median'];price=r.get('close')
        # Banking/insurance/securities and loss-makers need their own valuation
        # model; P/E peer multiples are not forced onto those sectors.
        sector=r.get('industry') or ''
        if sector and not any(x in sector for x in ('银行','保险','证券','多元金融')) and eps and eps>0 and price and price>0:
            pe=price/eps
            if r['consensus']['institutions']>=config['min_institutions'] and pe<=config['valuation_pe_cap']:
                peers[sector].append((r['stock_code'],pe))
    result=[]
    for r in rows:
        c=r['consensus'];eps=c['eps_median'];close=r.get('close');sector=r.get('industry') or ''
        group=[pe for code,pe in peers.get(sector,[]) if code!=r['stock_code']]
        reasons=[]
        if not close or close<=0:reasons.append('缺少有效的最新收盘价')
        if not eps or eps<=0:reasons.append('缺少可用的正盈利预测')
        if c['institutions']<config['min_institutions']:reasons.append('独立机构预测覆盖不足')
        if c.get('eps_low') is not None and c['eps_low']<=0:reasons.append('机构预测中仍包含亏损或零盈利')
        if len(group)<config['min_industry_peers']:reasons.append('同业估值样本不足或行业需专用估值模型')
        if c['conflicts']:reasons.append('同日预测存在来源冲突')
        multiple=min(median(group),config['valuation_pe_cap']) if len(group)>=config['min_industry_peers'] else None
        fair=eps*multiple if eps and eps>0 and multiple else None
        scenarios=None
        if fair and c['eps_low']>0:
            scenarios={'kind':'relative_valuation_not_price_forecast','bear':round(c['eps_low']*multiple*.8,2),
                'base':round(fair,2),'bull':round(c['eps_high']*multiple,2),'pe_multiple':multiple,
                'peer_count':len(group),'fiscal_year':c['fiscal_year'],'probabilities':None,'expected_hit_date':None}
        result.append({**r,'forward_pe':close/eps if close and eps and eps>0 else None,
            'valuation_scenarios':scenarios,'valuation_gaps':reasons,'fair_value':fair,
            'entry_limit':math.floor(min(fair*(1-config['entry_discount']),close*(1-config['no_chase_discount']))*100)/100
                if not reasons and fair and close else None})
    return result

def qualify(metrics, baseline, quality, shadow_sessions, config):
    g=config['gates'];annual=metrics.get('annual',{})
    checks={'net_return':metrics.get('cagr',-1)>g['cagr_min'],
        'drawdown':metrics.get('maximum_drawdown',1)<=g['max_drawdown_max'],
        'win_rate':(metrics.get('period_win_rate') or 0)>=g['win_rate_min'],
        'complete_years':sum(v.get('complete') is True and v['return']>0 for v in annual.values())>=g['positive_complete_years_min'],
        'baseline_return':metrics.get('cagr',-1)-baseline.get('cagr',1)>g['baseline_cagr_advantage_min'],
        'baseline_win':(metrics.get('period_win_rate') or 0)-(baseline.get('period_win_rate') or 1)>g['baseline_win_advantage_min'],
        'historical_execution':quality.get('strict_execution_verified') is True,
        'point_in_time':quality.get('point_in_time_verified') is True,
        'calibration':quality.get('forecast_calibration_passed') is True,
        'forward_shadow':shadow_sessions>=g['forward_shadow_sessions_min'] and quality.get('mature_forward_trades',0)>=30}
    return {'passed':all(checks.values()),'checks':checks,'failed':[k for k,v in checks.items() if not v]}

def build_plans(rows,previous,decision_at,published_at,sessions,config,approved=False):
    if stamp(decision_at)>stamp(published_at):raise ValueError('publication precedes decision')
    first=first_eligible_session(published_at,sessions)
    executable=[str(d)[:10] for d in sessions if first and str(d)[:10]>=first]
    expiry=executable[min(len(executable),config['entry_valid_sessions'])-1] if executable else None
    previous={p['stock_code']:p for p in previous};result=[]
    for r in rows:
        code=r['stock_code'];old=previous.pop(code,None)
        gaps=list(r['valuation_gaps'])
        if not r.get('financial_eligible'):gaps.append('财务质量尚不满足选入条件')
        if r['consensus']['revision_median'] is None:gaps.append('缺少同机构、同财年的盈利修正对比')
        elif r['consensus']['revision_median']<0:gaps.append('近期盈利预期下修')
        if first is None:gaps.append('缺少后续已核实交易日')
        if len(executable)<config['entry_valid_sessions']:gaps.append('交易日历不足以确定限价有效期')
        state='observe' if not gaps else 'data_review'
        # Shadow can record a proposed plan without labelling it a live buy.
        if approved and not gaps:state='eligible_to_buy'
        if old and old.get('paper_quantity',0)>0:state='held' if not gaps else 'data_review'
        plan={'stock_code':code,'name':r.get('name'),'state':state,'previous_plan_id':old.get('plan_id') if old else None,
            'previous_state':old.get('state') if old else None,'decision_at':str(decision_at),'published_at':str(published_at),
            'market_date':str(r['market_date']),'first_eligible_session':first,'entry_expires_on':expiry,
            'entry_limit':r['entry_limit'],'valuation':r['valuation_scenarios'],
            'expected_net_return':None,'target_hit_probability':None,'expected_sell_date':None,
            'review_horizons_sessions':config['holding_sessions'],'evidence':r['consensus'],
            'forward_pe':r['forward_pe'],'industry':r.get('industry'),
            'reason':gaps or ['相对估值折价且盈利修正未走弱，进入模拟观察'],
            'invalidation':['盈利预期转为下修','财务或证券身份依据失效','限价有效期结束'],
            'candidate_membership_is_position':False,'paper_only':not approved,
            'simulation_eligible':not gaps,
            'paper_quantity':old.get('paper_quantity',0) if old else 0,
            'position_scope':'simulated_reference_account',
            'cost_break_even':None,'change':'added' if old is None else 'retained'}
        if old:
            fields=('state','entry_limit','reason','valuation')
            if any(old.get(k)!=plan[k] for k in fields):plan['change']='revised'
        result.append(plan)
    for code,old in previous.items():
        # Removal cancels unfilled interest. A real/paper holding requires a
        # separate position review; no fabricated fill or break-even promise.
        if old['state']=='removed':continue
        held=old['state'] in ('held','reduce','exit')
        result.append({**old,'stock_code':code,'previous_plan_id':old.get('plan_id'),'state':'data_review' if held else 'removed',
            'decision_at':str(decision_at),'published_at':str(published_at),'change':'removed',
            'entry_limit':None,'reason':['退出候选范围；已有持仓需要单独复核' if held else '退出候选范围，取消未成交买入计划'],
            'paper_only':not approved,'simulation_eligible':False})
    for r in result:
        r.pop('plan_id',None);r['plan_id']=digest(r)
    return result

def render(plans,report_date,approved=False):
    title=f'{report_date} 候选池更新'+('' if approved else '（模拟观察）')
    lines=[title,'价格单位：元；时间：北京时间。']
    if not approved:lines.append('目前记录候选与估值观察，买卖提醒等待近五年回测和前向记录验收。')
    changed=[p for p in plans if p['change']!='retained']
    if not changed:lines.append('今日候选与计划无变化。')
    labels={'data_review':'待补充依据','observe':'观察','eligible_to_buy':'限价买入候选','removed':'移出候选','held':'继续持有','reduce':'减仓复核','exit':'退出复核'}
    for p in changed:
        lines.append(f"{p.get('name') or ''}（{p['stock_code']}）：{labels.get(p['state'],p['state'])}。"+'；'.join(p['reason'])+'。')
        if approved and p['state']=='eligible_to_buy':
            lines.append(f"买入上限 {p['entry_limit']:.2f}，有效至 {p['entry_expires_on']}；未到价格则不成交。")
    return '\n'.join(lines)
