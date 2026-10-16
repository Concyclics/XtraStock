"""Causal daily-bar limit matching shared by paper replay and plan validation.

Touch alone is not queue priority: require a one-tick penetration. Daily volume
is an ex-post capacity cap, never a signal input. Unknown intraday paths use
the adverse exit when both stop and take-profit prices are crossed.
"""
from __future__ import annotations
import datetime as dt
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')

def number(value):
    result=Decimal(str(value))
    if not result.is_finite(): raise ValueError('nonfinite execution input')
    return result

def stamp(value):
    value=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if value.tzinfo is None: raise ValueError('timestamp must include timezone')
    return value.astimezone(TZ)

def first_eligible_session(published_at, sessions):
    """Only before the opening auction; later delivery moves to the next day."""
    published=stamp(published_at)
    for session in sessions:
        day=dt.date.fromisoformat(str(session)[:10])
        if published < dt.datetime.combine(day,dt.time(9,15),TZ): return day.isoformat()
    return None

def match_limit(order, bar, session, slippage_bps=0):
    day=dt.date.fromisoformat(str(session)[:10])
    decided=stamp(order['decision_at']);published=stamp(order['published_at'])
    if decided>published: raise ValueError('publication precedes decision')
    if published>=dt.datetime.combine(day,dt.time(9,15),TZ):
        raise ValueError('order was not published before eligible auction')
    if str(order['session'])[:10]!=day.isoformat(): raise ValueError('order session mismatch')
    if order.get('expires_on') and day.isoformat()>str(order['expires_on'])[:10]:
        return None,'expired'
    side=order['side']
    if side not in ('buy','sell'): raise ValueError('invalid side')
    tick=number(bar.get('tick_size','.01'));limit=number(order['limit_price'])
    if tick<=0 or limit<=0 or limit%tick: raise ValueError('invalid limit price tick')
    required=('open','low','high','close','limit_up','limit_down')
    if any(bar.get(k) is None for k in required): return None,'missing_execution_rule'
    o,lo,hi,c,up,down=(number(bar[k]) for k in required)
    if not 0<lo<=min(o,c)<=max(o,c)<=hi or not 0<down<=up:
        raise ValueError('invalid bar or price limits')
    if limit>up or limit<down: return None,'limit_outside_exchange_band'
    if bar.get('suspended') or number(bar.get('amount',0))<=0: return None,'suspended_or_no_turnover'
    # No queue inference at the daily price limit, even when there is volume.
    if side=='buy' and lo>=up or side=='sell' and hi<=down: return None,'locked_price_limit'
    slip=number(slippage_bps)/10000
    if not 0<=slip<=Decimal('.1'): raise ValueError('invalid slippage')
    if side=='buy':
        if lo>limit-tick: return None,'limit_not_penetrated'
        px=min(limit,o*(1+slip)) if o<limit else limit
        px=max(lo,min(hi,px))
        px=(px/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick
        px=min(px,limit)
    else:
        if hi<limit+tick: return None,'limit_not_penetrated'
        px=max(limit,o*(1-slip)) if o>limit else limit
        px=max(lo,min(hi,px))
        px=(px/tick).to_integral_value(rounding=ROUND_HALF_UP)*tick
        px=max(px,limit)
    return px,'opening_price' if (side=='buy' and o<limit or side=='sell' and o>limit) else 'intraday_limit'

def exit_barrier(bar, stop, target, bought_on, session):
    """Diagnostic OCO replay: never claim the favourable path for an unknown path."""
    if str(bought_on)[:10]>=str(session)[:10]: return None,'t_plus_one'
    stop,target=number(stop),number(target)
    if not 0<stop<target: raise ValueError('invalid exit barriers')
    o,lo,hi=(number(bar[k]) for k in ('open','low','high'))
    if bar.get('suspended'): return None,'suspended'
    if bar.get('limit_down') is None: return None,'missing_execution_rule'
    if hi<=number(bar['limit_down']): return None,'locked_limit_down'
    if o<=stop: return o,'stop_gap'
    if o>=target: return target,'target_at_open_conservative'
    if lo<=stop: return stop,'both_touched_adverse' if hi>=target else 'stop'
    if hi>=target: return target,'target'
    return None,'neither_touched'
