"""Daily limit-order research cashbook. No broker, model call or message sends.

Research reconstruction explicitly records approximate price-limit/lifecycle
rules. It cannot promote a production plan. Entry limits and order quantities
are fixed before the execution day; intraday sell proceeds cannot fund buys.
"""
from __future__ import annotations
from collections import Counter
import math
import numpy as np
try:
    from .limit_execution import match_limit
    from .strategy_research import action_schedule,liquidity_amount,metrics
except ImportError:
    from limit_execution import match_limit
    from strategy_research import action_schedule,liquidity_amount,metrics

MAINBOARD=('000','001','002','003','600','601','603','605')

def replay(d,f,scores,entry_limits,start,end,*,horizon=20,fee_bps=15,initial_cash=1_000_000,
           size=20,expiry=3,participation=.01):
    if horizon not in (20,60) or not 1<=expiry<=20 or fee_bps<0 or initial_cash<=0:
        raise ValueError('invalid replay configuration')
    if np.shape(scores)!=d['close'].shape or np.shape(entry_limits)!=d['close'].shape:
        raise ValueError('signal and price axes must align')
    dates=d['dates'];indices=np.flatnonzero((dates>=start)&(dates<=end))
    if not len(indices) or indices[0]==0 or start<d.get('primary_since',start):raise ValueError('invalid primary history')
    cash=float(initial_cash);n=len(d['symbols']);shares=np.zeros(n);cost=np.zeros(n);receipts=np.zeros(n)
    entries=np.full(n,-1);last=np.nan_to_num(d['close'][indices[0]-1],nan=0.);stale=np.zeros(n,int)
    pending={};history=[];fills=[];closed=[];nav=[cash];nav_rows=[];counters=Counter();payables={};receivable=0.
    actions,gaps=action_schedule(d);amount,_=liquidity_amount(d);rate=fee_bps/10000
    mainboard=np.array([str(s).startswith(MAINBOARD) for s in d['symbols']])
    for t in indices:
        s=t-1;day=str(dates[t]);before_cash=cash;reference=last.copy()
        for j,extra,dividend,pay,quality in actions.get(int(t),[]):
            reference[j]=(reference[j]-dividend)/(1+extra) if reference[j]>0 else 0
            last[j]=reference[j]
            if j in pending:
                history.append({**pending.pop(j),'event':'cancelled','event_date':day,'reason':'corporate_action_reprice_required'})
            if shares[j]>0:
                entitlement=shares[j]*dividend*.8
                shares[j]*=1+extra;receipts[j]+=entitlement;receivable+=entitlement
                payables[pay]=payables.get(pay,0)+entitlement
                if quality!='verified':counters['unverified_corporate_actions_held']+=1
        paid=payables.pop(int(t),0);cash+=paid;receivable-=paid
        before_cash=cash
        equity=cash+float(shares@last)+receivable
        eligible=np.isfinite(scores[s])&np.isfinite(entry_limits[s])&(entry_limits[s]>0)&mainboard
        selected=np.flatnonzero(eligible);selected=selected[np.argsort(-scores[s,selected],kind='stable')[:size]]
        selected=set(map(int,selected))
        for j,o in list(pending.items()):
            if t>o['expiry_index'] or (o['side']=='buy' and j not in selected):
                reason='expired' if t>o['expiry_index'] else 'removed_from_candidate_pool'
                history.append({**pending.pop(j),'event':'cancelled','event_date':day,'reason':reason});counters[reason]+=1
        # Fixed existing holding: exit at horizon or lost fundamental signal.
        # This is a daily exit decision, not an assumed intraday stop-loss fill.
        for j in np.flatnonzero(shares>1e-8):
            if j in pending:continue
            if t-entries[j]>=horizon or not eligible[j]:
                price=math.floor(reference[j]*.99*100)/100
                if price>0:
                    pending[int(j)]={'order_id':f'{day}:{d["symbols"][j]}:sell','code':str(d['symbols'][j]),'side':'sell',
                        'quantity':float(shares[j]),'remaining':float(shares[j]),'limit_price':price,'created_index':int(t),
                        'expiry_index':int(t+expiry-1),'decision_at':str(dates[s])+'T16:00:00+08:00',
                        'published_at':day+'T09:05:00+08:00','reason':'time_exit' if t-entries[j]>=horizon else 'fundamental_signal_invalidated'}
                    history.append({**pending[int(j)],'event':'order','event_date':day})
        slots=max(0,size-int(np.sum(shares>1e-8))-sum(o['side']=='buy' for o in pending.values()))
        for j in sorted(selected,key=lambda j:(-scores[s,j],str(d['symbols'][j]))):
            if slots<=0:break
            if shares[j]>1e-8 or j in pending:continue
            limit=math.floor(float(entry_limits[s,j])*100)/100
            qty=math.floor(equity/size/limit/100)*100
            if qty<=0:continue
            pending[j]={'order_id':f'{day}:{d["symbols"][j]}:buy','code':str(d['symbols'][j]),'side':'buy',
                'quantity':qty,'remaining':qty,'limit_price':limit,'created_index':int(t),'expiry_index':int(t+expiry-1),
                'decision_at':str(dates[s])+'T16:00:00+08:00','published_at':day+'T09:05:00+08:00','reason':'frozen_valuation_and_revision'}
            history.append({**pending[j],'event':'order','event_date':day});slots-=1
        capacity={};buy_budget=before_cash
        for j,o in sorted(list(pending.items()),key=lambda item:(item[1]['side']=='buy',item[1]['order_id'])):
            if not f['valid'][t,j] or not math.isfinite(d['volume'][t,j]) or d['volume'][t,j]<=0 or reference[j]<=0:
                counters['missing_or_suspended_execution_days']+=1;continue
            bar={k:float(d[k][t,j]) for k in ('open','high','low','close')}
            bar.update(limit_up=round(reference[j]*1.10,2),limit_down=round(reference[j]*.9,2),amount=float(amount[t,j]))
            gap=bar['open']/reference[j]-1
            if o['side']=='buy' and gap>=.048 or o['side']=='sell' and gap<=-.048:
                counters['conservative_opening_gap_rejection']+=1;continue
            px,reason=match_limit({**o,'session':day},bar,day,5)
            if px is None:counters[reason]+=1;continue
            px=float(px)
            # Both lagged liquidity and ex-post share/notional caps must hold.
            capacity.setdefault(j,max(0,min(float(f['adv'][s,j]),float(amount[s,j]),float(amount[t,j]))*participation))
            max_qty=min(capacity[j]/px,float(d['volume'][t,j])*100*participation)
            if o['side']=='buy':
                qty=math.floor(min(o['remaining'],max_qty,buy_budget/(px*(1+rate)))/100)*100
            else:
                if entries[j]>=t:counters['t_plus_one']+=1;continue
                qty=min(o['remaining'],max_qty,shares[j])
                if qty<shares[j]-1e-8:qty=math.floor(qty/100)*100
            if qty<=0:counters['cash_or_capacity_rejection']+=1;continue
            gross=qty*px;fee=gross*rate
            if o['side']=='buy':
                cash-=gross+fee;buy_budget-=gross+fee;shares[j]+=qty;cost[j]+=gross+fee
                if entries[j]<0:entries[j]=t
            else:
                cash+=gross-fee;shares[j]-=qty;receipts[j]+=gross-fee
                if shares[j]<1e-8:
                    closed.append({'code':str(d['symbols'][j]),'entry_date':str(dates[entries[j]]),'exit_date':day,
                        'cost':float(cost[j]),'net_pnl':float(receipts[j]-cost[j]),'holding_sessions':int(t-entries[j])})
                    shares[j]=0;entries[j]=-1;cost[j]=0;receipts[j]=0
            capacity[j]-=gross;o['remaining']-=qty
            fill={**o,'event':'fill','event_date':day,'fill_qty':float(qty),'price':px,'gross':gross,'fees':fee,
                'matching_assumption':reason,'rule_quality':'historical_mainboard_10pct_proxy',
                'daily_volume_shares':float(d['volume'][t,j]*100),'participation':participation}
            fills.append(fill);history.append(fill)
            if o['remaining']<=1e-8:pending.pop(j)
        valid=f['valid'][t]
        last=np.where(valid,d['close'][t],last);stale=np.where(valid,0,stale+1)
        if cash<-.01 or np.any(shares<-.000001):raise ValueError('cash/share conservation failed')
        marked=float(shares@last);wealth=cash+marked+receivable
        if wealth<=0 or not math.isfinite(wealth):raise ValueError('unusable marked NAV')
        stale_value=float((shares*np.where(stale>0,last,0)).sum())
        nav.append(wealth);nav_rows.append({'date':day,'cash':cash,'positions':marked,'receivable':receivable,'nav':wealth,'stale_value':stale_value})
        counters['maximum_stale_sessions']=max(counters['maximum_stale_sessions'],int(np.max(stale[shares>1e-8],initial=0)))
    # Open inventory is marked and charged an exit-fee provision, never labelled sold.
    provision=float(shares@last)*rate;nav[-1]-=provision
    m=metrics(nav,dates[indices],20)
    for year,annual in m['annual'].items():annual['complete']=start<=year+'-01-01' and end>=year+'-12-31'
    orders=[o for o in history if o['event']=='order'];filled_ids={o['order_id'] for o in fills}
    m.update(order_count=len(orders),filled_order_count=len(filled_ids),fill_rate=len(filled_ids)/len(orders) if orders else 0,
        fill_events=len(fills),closed_trades=len(closed),closed_trade_win_rate=float(np.mean([x['net_pnl']>0 for x in closed])) if closed else None,
        fees=sum(x['fees'] for x in fills),terminal_exit_fee_provision=provision,terminal_open_positions=int(np.sum(shares>1e-8)),
        terminal_stale_value=nav_rows[-1]['stale_value'],maximum_stale_sessions=counters['maximum_stale_sessions'])
    return {'metrics':m,'dates':dates[indices].tolist(),'nav':nav,'daily_account':nav_rows,'events':history,'fills':fills,
        'closed_trades':closed,'pending_at_end':list(pending.values()),'diagnostics':dict(counters),'action_gaps':gaps,
        'quality':{'strict_execution_verified':False,'point_in_time_verified':False,'forecast_calibration_passed':False},
        'limitations':['Historical ST/limit prices and order queues are incomplete; mainboard limits are research approximations.',
            'Dated document estimates reconstruct public knowledge, not the historical system snapshot.',
            'Missing held marks remain at the last observation and are explicitly flagged; delisting settlements are incomplete.',
            'Observed current API estimates are excluded from every historical signal.'],
        'production_approved':False}
