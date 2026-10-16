"""Settle one verified day of the new simulated account; never place orders.

Missing held marks/actions abort the transaction and leave the day unapplied.
The exact rejection is persisted as a review event, not a fabricated NAV.
"""
from __future__ import annotations
import argparse
import datetime as dt
import json
import math
try:
    from .common import connect,jsonb
    from .value_plan import load_config,digest,stamp
    from .paper_execution import step,DataGap
    from .run_quant_paper_daily import paper_bars
    from .limit_execution import first_eligible_session
except ImportError:
    from common import connect,jsonb
    from value_plan import load_config,digest,stamp
    from paper_execution import step,DataGap
    from run_quant_paper_daily import paper_bars
    from limit_execution import first_eligible_session

ACCOUNT='daily-value-plan-shadow-v1'

def orders_for_session(state,plans,day,sessions,prices,config):
    day=str(day);by_code={p['stock_code']:p for p in plans};orders=[];review=[];pending=[]
    quantities={}
    for lot in state.get('lots',[]):quantities[lot['code']]=quantities.get(lot['code'],0)+int(lot['qty'])
    for old in state.get('pending_limits',[]):
        p=by_code.get(old['code'])
        if day>old['expires_on'] or not p or not p.get('simulation_eligible'):
            review.append({'plan_id':old['plan_id'],'event_type':'cancelled','reason':'expired_or_new_plan_invalidated','order_id':old['id']})
        else:pending.append({**old,'session':day})
    existing={o['code'] for o in pending}
    for p in plans:
        code=p['stock_code'];first=first_eligible_session(p['published_at'],sessions)
        if code in existing or code in quantities or not p.get('simulation_eligible') or not first or day<first:
            continue
        if not p.get('entry_expires_on') or day>p['entry_expires_on'] or not p.get('entry_limit'):continue
        quantity=math.floor(float(config['initial_paper_cash'])/config['max_candidates']/p['entry_limit']/100)*100
        if quantity:
            pending.append({'id':digest([p['plan_id'],'entry']),'plan_id':p['plan_id'],'code':code,'side':'buy','qty':quantity,
                'session':day,'decision_at':p['decision_at'],'published_at':p['published_at'],'limit_price':p['entry_limit'],
                'expires_on':p['entry_expires_on']})
    positions_by_code={}
    for lot in state.get('lots',[]):positions_by_code.setdefault(lot['code'],[]).append(lot)
    for code,lots in positions_by_code.items():
        oldest=min(str(l['bought_on']) for l in lots)
        held_sessions=sum(oldest<str(s)<=day for s in sessions)
        if held_sessions<min(config['holding_sessions']):continue
        plan=by_code.get(code)
        if not plan:
            review.append({'event_type':'review','reason':'held_position_missing_current_plan','code':code});continue
        reference=prices.get(code)
        if not reference:raise DataGap('missing prior close for time-exit limit: '+code)
        pending=[o for o in pending if o['code']!=code]
        # A fixed holding-period policy is known before the session opens.
        before=dt.date.fromisoformat(day)-dt.timedelta(days=1)
        pending.append({'id':digest([plan['plan_id'],'time-exit',day]),'plan_id':plan['plan_id'],'code':code,'side':'sell',
            'qty':quantities[code],'session':day,'decision_at':before.isoformat()+'T23:59:00+08:00',
            'published_at':before.isoformat()+'T23:59:00+08:00','limit_price':math.floor(reference*.99*100)/100,
            'expires_on':day})
    orders.extend(pending)
    return orders,review

def settle(conn,day,config):
    if conn.execute('SELECT 1 FROM quant.daily_plan_sessions WHERE account_id=%s AND session_date=%s',(ACCOUNT,day)).fetchone():
        return {'status':'already_settled'}
    calendar=conn.execute('''SELECT trade_date,close_at FROM market.trading_calendar WHERE exchange='SSE' AND is_verified
       AND session_status='open' AND trade_date BETWEEN %s::date-180 AND %s::date+30 ORDER BY trade_date''',(day,day)).fetchall()
    session=next((r for r in calendar if r['trade_date']==day),None)
    if not session:return {'status':'not_verified_trading_session'}
    if session['close_at'] is None or session['close_at']>dt.datetime.now(dt.timezone.utc):raise DataGap('session has not closed')
    account=conn.execute('SELECT state FROM quant.daily_plan_accounts WHERE account_id=%s FOR UPDATE',(ACCOUNT,)).fetchone()
    state=account['state'] if account else {'cash':str(config['initial_paper_cash']),'lots':[],'pending_limits':[]}
    dates=[str(r['trade_date']) for r in calendar];previous=max(s for s in dates if s<str(day))
    if state.get('last_session') and state['last_session']!=previous:raise DataGap('paper sessions must be replayed in sequence')
    rows=conn.execute('''SELECT p.payload FROM quant.daily_plan_revisions p JOIN quant.daily_plan_runs r USING(run_id)
       WHERE r.mode='shadow' AND r.report_date=(SELECT max(report_date) FROM quant.daily_plan_runs
       WHERE mode='shadow' AND decision_at<%s)''',(str(day)+'T09:15:00+08:00',)).fetchall()
    plans=[r['payload'] for r in rows]
    if not plans:return {'status':'no_prior_plan'}
    codes=list({p['stock_code'] for p in plans}|{l['code'] for l in state['lots']})
    prices={r['symbol']:float(r['close']) for r in conn.execute('''SELECT a.symbol,b.close FROM market.assets a
       JOIN market.canonical_bars_daily_v2 b USING(asset_id) WHERE a.symbol=ANY(%s) AND a.asset_type='stock'
       AND b.dt=%s AND b.quality_status='verified' AND b.available_at<=%s''',(codes,previous,str(day)+'T09:15:00+08:00'))}
    orders,review=orders_for_session(state,plans,day,dates,prices,config)
    needed=list({o['code'] for o in orders}|{l['code'] for l in state['lots']})
    bars,actions=paper_bars(conn,needed,day)
    policy={'buy_fee_bps':config['cost_bps_per_side'][0],'sell_fee_bps':config['cost_bps_per_side'][0],
        'slippage_bps':config['slippage_bps'],'max_participation':config['max_participation'],
        'no_intraday_sale_funding':True,'track_cost_basis':True}
    result=step(state,day,bars,orders,policy,actions)
    result['state']['pending_limits']=[{**o,'qty':o['qty']-o['filled_qty']} for o in result['orders']
        if o['filled_qty']<o['qty'] and str(day)<o['expires_on']]
    result['review']=review
    conn.execute('''INSERT INTO quant.daily_plan_accounts(account_id,initial_cash,state,policy) VALUES(%s,%s,%s,%s)
       ON CONFLICT(account_id) DO UPDATE SET state=excluded.state,policy=excluded.policy,updated_at=now()''',
       (ACCOUNT,config['initial_paper_cash'],jsonb(result['state']),jsonb(policy)))
    conn.execute('INSERT INTO quant.daily_plan_sessions(account_id,session_date,result) VALUES(%s,%s,%s)',(ACCOUNT,day,jsonb(result)))
    for o in result['orders']:
        outcome=[] if o['status']=='filled' else [('partial' if o['filled_qty'] else 'expired',{**o,'expiry_scope':'DAY; overall limit-plan expiry is expires_on'})]
        for kind,payload in [('order',o),*outcome,*[( 'fill',f) for f in result['fills'] if f['order_id']==o['id']]]:
            conn.execute('''INSERT INTO quant.daily_plan_events(event_id,plan_id,event_type,event_at,payload)
               VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
               (digest([ACCOUNT,str(day),o['id'],kind]),o['plan_id'],kind,session['close_at'],jsonb(payload)))
    for event in review:
        if event.get('plan_id'):
            conn.execute('''INSERT INTO quant.daily_plan_events(event_id,plan_id,event_type,event_at,payload)
              VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
              (digest([ACCOUNT,str(day),event]),event['plan_id'],event['event_type'],session['close_at'],jsonb(event)))
    return {'status':'settled','orders':len(orders),'fills':len(result['fills']),'nav':result['nav']}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--date',required=True);a=p.parse_args()
    with connect(autocommit=False) as conn:
        conn.execute("SET LOCAL statement_timeout='90s'");conn.execute("SET LOCAL lock_timeout='3s'")
        conn.execute('SELECT pg_advisory_xact_lock(704201612)')
        try:
            with conn.transaction():result=settle(conn,dt.date.fromisoformat(a.date),load_config())
        except DataGap as exc:
            result={'status':'review_required','reason':str(exc),'nav_written':False}
            plan=conn.execute('''SELECT p.plan_id FROM quant.daily_plan_revisions p JOIN quant.daily_plan_runs r USING(run_id)
              WHERE r.report_date<=%s ORDER BY r.decision_at DESC,p.stock_code LIMIT 1''',(a.date,)).fetchone()
            if plan:conn.execute('''INSERT INTO quant.daily_plan_events(event_id,plan_id,event_type,event_at,payload)
              VALUES(%s,%s,'review',now(),%s) ON CONFLICT DO NOTHING''',
              (digest([ACCOUNT,a.date,result]),plan['plan_id'],jsonb(result)))
    print(json.dumps(result,ensure_ascii=False));return 2 if result['status']=='review_required' else 0

if __name__=='__main__':raise SystemExit(main())
