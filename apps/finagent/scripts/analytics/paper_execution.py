"""Deterministic long-only paper execution. No broker or network capability.

Prices are unadjusted CNY. Limits must come from a verified instrument/day rule.
Fees are explicit research assumptions, not a statement of any broker tariff.
Missing valuations/unsupported corporate actions block the whole atomic step.
"""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import datetime as dt

VERSION='paper-ledger-v1'
class DataGap(ValueError): pass

def dec(v):
    d=Decimal(str(v))
    if not d.is_finite(): raise DataGap('non-finite numeric input')
    return d

def money(v): return dec(v).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
def date(v): return dt.date.fromisoformat(str(v)[:10])

def default_policy():
    return {'buy_fee_bps':10,'sell_fee_bps':15,'slippage_bps':5,
            'max_participation':.01,'cost_basis':'illustrative all-in costs; broker calibration required',
            'capacity_basis':'ex-post daily turnover proxy; not verified opening auction liquidity'}

def step(state,session,bars,orders=(),policy=None,actions=()):
    """Pure atomic transition; caller persists returned state only on success.

    An order is DAY-valid with explicit session and frozen decision timestamp.
    Partials/unfilled orders are logged and expire; later rebalance may reissue.
    Held suspended stocks need an explicit last close/valuation policy, never zero.
    """
    policy=policy or default_policy();today=date(session);s=deepcopy(state)
    if any(not 0<=dec(policy[k])<=1000 for k in ('buy_fee_bps','sell_fee_bps','slippage_bps')) or not 0<dec(policy['max_participation'])<=1:
        raise DataGap('invalid cost or participation policy')
    if s.get('last_session') and today<=date(s['last_session']):
        raise DataGap('session already applied or out of order')
    cash=dec(s.get('cash',0)); lots=s.setdefault('lots',[])
    if cash<0:raise DataGap('negative cash')
    for lot in lots:
        if int(lot['qty'])<=0 or date(lot['bought_on'])>today:raise DataGap('invalid holding lot')
    receivables=s.setdefault('receivables',[])
    paid=set(s.get('applied_actions',[]))
    for action in actions:
        aid=str(action['id']);code=action['code'];qty=sum(int(l['qty']) for l in lots if l['code']==code)
        if aid in paid:continue
        if date(action['ex_date'])!=today:raise DataGap('corporate action outside current session')
        if action.get('quality')!='verified':raise DataGap('unverified corporate action')
        if action['kind']=='cash_dividend':
            if not action.get('pay_date'):raise DataGap('cash dividend payment date unknown')
            if date(action['pay_date'])<today:raise DataGap('dividend payment before ex-date')
            # Ex-date purchasers are not entitled; account already holds prior-close lots.
            entitled=sum(int(l['qty']) for l in lots if l['code']==code and date(l['bought_on'])<today)
            receivables.append({'id':aid,'amount':str(money(entitled*dec(action['per_share']))),'pay_date':str(action['pay_date'])})
        elif action['kind']=='split':
            ratio=dec(action['ratio'])
            if ratio<=0:raise DataGap('invalid split ratio')
            for l in lots:
                if l['code']==code:
                    new_qty=dec(l['qty'])*ratio
                    if new_qty!=new_qty.to_integral_value():raise DataGap('fractional split needs cash-in-lieu policy')
                    l['qty']=int(new_qty)
        else:raise DataGap('unsupported corporate action: '+action['kind'])
        paid.add(aid)
    remain=[]
    for r in receivables:
        if date(r['pay_date'])<=today:cash+=dec(r['amount'])
        else:remain.append(r)
    receivables=remain
    for code in {l['code'] for l in lots}:
        bar=bars.get(code)
        if not bar or bar.get('close') is None or dec(bar['close'])<=0:raise DataGap('held security lacks valuation: '+code)
        if bar.get('quality')!='verified':raise DataGap('held security valuation not verified: '+code)
    fills=[];results=[];seen=set();used_capacity={}
    ordered=sorted(orders,key=lambda o:(0 if o['side']=='sell' else 1,str(o['id'])))
    for o in ordered:
        oid=str(o['id']);code=o['code'];side=o['side'];qty=int(o['qty'])
        if oid in seen:raise DataGap('duplicate order id')
        seen.add(oid)
        if side not in ('buy','sell') or qty<=0:raise DataGap('invalid order')
        decision=dt.datetime.fromisoformat(str(o['decision_at']).replace('Z','+00:00'))
        if decision.tzinfo is None:raise DataGap('decision timezone missing')
        from zoneinfo import ZoneInfo
        if date(o['session'])!=today or decision.astimezone(ZoneInfo('Asia/Shanghai')).date()>=today:
            raise DataGap('order must follow its frozen decision date')
        result={**o,'filled_qty':0,'status':'expired','reason':None}
        b=bars.get(code)
        if not b or b.get('quality')!='verified' or b.get('amount') is None:
            result['reason']='missing_verified_execution_data';results.append(result);continue
        if b.get('suspended') or dec(b.get('amount',0))<=0:
            result['reason']='suspended_or_no_turnover';results.append(result);continue
        required=['open','high','low','close','limit_up','limit_down','lot_size']
        if any(b.get(k) is None for k in required):
            result['reason']='execution_rule_unavailable';results.append(result);continue
        op,hi,lo,close=map(lambda k:dec(b[k]),['open','high','low','close'])
        if not (0<lo<=op<=hi and lo<=close<=hi):raise DataGap('invalid OHLC')
        up,down=dec(b['limit_up']),dec(b['limit_down']);lot=int(b['lot_size'])
        if lot<=0 or down<=0 or up<=down:raise DataGap('invalid execution rule')
        if (side=='buy' and op>=up) or (side=='sell' and op<=down):
            result['reason']='opening_price_limit_no_assumed_fill';results.append(result);continue
        slip=dec(policy['slippage_bps'])/10000
        px=money(min(hi,up,op*(1+slip)) if side=='buy' else max(lo,down,op*(1-slip)))
        remaining_capacity=max(Decimal(0),dec(b['amount'])*dec(policy['max_participation'])-used_capacity.get(code,Decimal(0)))
        cap=int(remaining_capacity/px)
        rate=dec(policy['buy_fee_bps'] if side=='buy' else policy['sell_fee_bps'])/10000
        if side=='buy':
            available=int(cash/(px*(1+rate)))
            n=min(qty,cap,available)//lot*lot
        else:
            available=sum(int(l['qty']) for l in lots if l['code']==code and date(l['bought_on'])<today)
            n=min(qty,cap,available)
            # Odd-lot disposal permitted only if clearing all currently sellable shares.
            if n<available:n=n//lot*lot
        if n<=0:
            result['reason']='cash_participation_or_tplus1_limit';results.append(result);continue
        gross=money(px*n);fee=money(gross*rate)
        used_capacity[code]=used_capacity.get(code,Decimal(0))+gross
        if side=='buy':
            cash-=gross+fee;lots.append({'code':code,'qty':n,'bought_on':str(today)})
        else:
            remaining=n
            for l in lots:
                if l['code']==code and date(l['bought_on'])<today:
                    take=min(remaining,int(l['qty']));l['qty']-=take;remaining-=take
                    if not remaining:break
            cash+=gross-fee;lots=[l for l in lots if l['qty']>0]
        result.update(filled_qty=n,status='filled' if n==qty else 'partial_expired',reason=None if n==qty else 'capacity_or_cash_limited')
        results.append(result);fills.append({'order_id':oid,'code':code,'side':side,'qty':n,'price':str(px),'fees':str(fee),'gross':str(gross)})
    positions={}
    for l in lots:positions[l['code']]=positions.get(l['code'],0)+int(l['qty'])
    value=sum((dec(bars[code]['close'])*qty for code,qty in positions.items()),Decimal(0))
    receivable=sum((dec(r['amount']) for r in receivables),Decimal(0))
    if cash<0:raise DataGap('cash conservation failure')
    s.update(cash=str(money(cash)),lots=lots,receivables=receivables,applied_actions=sorted(paid),last_session=str(today))
    return {'state':s,'orders':results,'fills':fills,'positions':positions,'cash':str(money(cash)),
            'receivable':str(money(receivable)),'nav':str(money(cash+value+receivable)),
            'turnover':str(sum((dec(f['gross']) for f in fills),Decimal(0))),'version':VERSION}
