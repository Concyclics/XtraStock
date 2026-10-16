#!/usr/bin/env python3
"""Record readiness and advance an explicitly paper-only account.

The data gate can prevent opening risk but never fabricates a successful NAV.
Pending orders were fixed before their execution date; no historical new signals.
"""
from __future__ import annotations
import argparse,datetime as dt,hashlib,json
from zoneinfo import ZoneInfo
try:
    from .common import connect,jsonb
    from .quant_readiness import collect,save
    from .paper_execution import step,DataGap,default_policy,dec,money
except ImportError:
    from common import connect,jsonb
    from quant_readiness import collect,save
    from paper_execution import step,DataGap,default_policy,dec,money

ACCOUNT='research-mainboard-weekly-v1'
TZ=ZoneInfo('Asia/Shanghai')

def assert_session_continuity(last_session, previous_session, is_open):
    if not is_open:raise DataGap('session is not a verified exchange trading day')
    if last_session and str(last_session)!=str(previous_session):
        raise DataGap('missing intermediate paper sessions; replay in order before advancing NAV')

def targets(rows,limit=30):
    """Frozen baseline ranks with bounded stock/sector weights; no LLM forecasts."""
    candidates=[]
    for r in rows:
        p=r['payload']
        frozen=p.get('input_contract',{})
        if frozen.get('version')!='snapshot-input-v1' or frozen.get('strict_pit_ready') is not True:continue
        if str(frozen.get('feature_updated_at'))!=str(r.get('feature_updated_at')):continue
        if not r.get('strict') or r.get('lifecycle_quality')!='verified' or not r.get('tradable'):continue
        if r.get('is_st') or r.get('is_suspended') or (r.get('amount') or 0)<20_000_000:continue
        code=p['stock_code']
        if not code.startswith(('600','601','603','605','000','001','002','003')):continue
        if not r.get('listing_verified') or (r.get('listed_sessions') or 0)<60:continue
        required={'momentum_20d','reversal_5d','low_volatility_20d'}
        if not required.issubset(set(frozen.get('eligible_score_names',[]))):continue
        factors=[x for x in p.get('factors',[]) if x.get('factor_name') in required and x.get('percentile') is not None]
        if len(factors)!=3 or {x['factor_name'] for x in factors}!=required:continue
        if any(x.get('source_cohort_hash')!=r.get('cohort_hash') or not r.get('cohort_hash')
            or x.get('source_factor_version') is None
            or x.get('source_factor_version')!=r.get('factor_versions',{}).get(x['factor_name']) for x in factors):continue
        values=[x['percentile'] for x in factors]
        if not r.get('close') or r['close']<=0:continue
        candidates.append((sum(values)/3,code,r))
    selected=[];sectors={};allocation=dec(0);weight=dec('.03')
    for score,code,r in sorted(candidates,key=lambda x:(-x[0],x[1])):
        sector=r.get('industry') or 'unclassified'
        if sectors.get(sector,dec(0))+weight>dec('.24'):continue
        selected.append({'code':code,'weight':float(weight),'score':score,'close':float(r['close']),'sector':sector})
        sectors[sector]=sectors.get(sector,dec(0))+weight;allocation+=weight
        if len(selected)>=limit or allocation>=dec('.90'):break
    return selected

try:
    from .lifecycle_evidence import lifecycle_order
except ImportError:
    from lifecycle_evidence import lifecycle_order

def paper_bars(conn,codes,day):
    if not codes:return {},[]
    rows=conn.execute(f"""WITH selected AS(SELECT * FROM market.assets WHERE asset_type='stock' AND symbol=ANY(%s))
       SELECT a.asset_id,a.symbol code,b.open,b.high,b.low,b.close,b.amount,b.quality_status,
       prev.close previous_close,l.is_st,l.is_suspended,l.quality_status lifecycle_quality,
       a.metadata->>'listing_date_verified'='true' listing_verified,
       (SELECT count(*) FROM market.trading_calendar tc WHERE tc.exchange='SSE' AND tc.is_verified
        AND tc.session_status='open' AND tc.trade_date BETWEEN a.listed_date AND %s) listed_sessions
       FROM selected a LEFT JOIN market.canonical_bars_daily_v2 b ON b.asset_id=a.asset_id AND b.dt=%s
       LEFT JOIN LATERAL(SELECT close FROM market.canonical_bars_daily_v2 p WHERE p.asset_id=a.asset_id
         AND p.quality_status='verified' AND p.dt=(SELECT max(trade_date) FROM market.trading_calendar
         WHERE exchange='SSE' AND is_verified AND session_status='open' AND trade_date<%s))prev ON true
       LEFT JOIN LATERAL(SELECT * FROM market.asset_lifecycle_versions l WHERE l.asset_id=a.asset_id
           AND l.valid_from<=%s AND (l.valid_to IS NULL OR l.valid_to>=%s) AND l.available_at<=now()
           ORDER BY {lifecycle_order('l')} LIMIT 1)l ON true""",(codes,day,day,day,day,day)).fetchall()
    out={};assets=[]
    for r in rows:
        code=r['code'];assets.append(r['asset_id'])
        if code in out:raise DataGap('ambiguous security identity: '+code)
        known=code.startswith(('600','601','603','605','000','001','002','003')) and r['lifecycle_quality']=='verified' and not r['is_st'] and r['listing_verified'] and r['listed_sessions']>=60
        prev=r['previous_close']
        out[code]={'open':r['open'],'high':r['high'],'low':r['low'],'close':r['close'],'amount':r['amount'],
          'quality':'verified' if r['quality_status']=='verified' else 'unverified','suspended':r['is_suspended'],
          'lot_size':100 if known else None,'limit_up':money(dec(prev)*dec('1.10')) if known and prev else None,
          'limit_down':money(dec(prev)*dec('.90')) if known and prev else None}
    actions=conn.execute("""SELECT c.*,a.symbol code FROM market.corporate_actions c JOIN market.assets a USING(asset_id)
      WHERE c.asset_id=ANY(%s) AND ex_date=%s AND c.quality_status<>'rejected' ORDER BY asset_id,action_id""",(assets,day)).fetchall() if assets else []
    # Correct ex-right limit reference requires a rule feed; do not derive from yesterday's raw close.
    if actions:raise DataGap('corporate action session requires verified ex-right price-limit adapter')
    return out,[]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--date');ap.add_argument('--readiness-only',action='store_true');ap.add_argument('--initial-paper-cash',type=float,default=1_000_000)
    args=ap.parse_args();today=dt.datetime.now(TZ).date();day=dt.date.fromisoformat(args.date) if args.date else today
    if day>today:raise SystemExit('future paper session is not allowed')
    with connect(autocommit=False) as c:
        c.execute("SET LOCAL statement_timeout='45s'");c.execute("SET LOCAL lock_timeout='5s'")
        e=collect(c,day);readiness=save(c,day,e);rid=readiness['readiness_id']
        summary={'date':str(day),'mode':'paper','readiness':readiness['status'],'checks':readiness['checks']}
        if args.readiness_only:
            c.commit();print(json.dumps(summary,ensure_ascii=False,default=str));return 0
        if not c.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) ok",(ACCOUNT,)).fetchone()['ok']:
            raise RuntimeError('paper account already processing')
        account=c.execute('SELECT * FROM quant.paper_accounts WHERE account_id=%s FOR UPDATE',(ACCOUNT,)).fetchone()
        if not account and readiness['status']=='ready':
            c.execute("""INSERT INTO quant.paper_accounts(account_id,initial_cash,policy,state,strategy_version)
               VALUES(%s,%s,%s,%s,'paper-ledger-v1')""",(ACCOUNT,args.initial_paper_cash,jsonb(default_policy()),jsonb({'cash':str(args.initial_paper_cash),'lots':[]})))
            account=c.execute('SELECT * FROM quant.paper_accounts WHERE account_id=%s FOR UPDATE',(ACCOUNT,)).fetchone()
        if not account:
            summary['paper_status']='waiting_for_verified_inputs';c.commit();print(json.dumps(summary,ensure_ascii=False,default=str));return 0
        done=c.execute('SELECT status FROM quant.paper_sessions WHERE account_id=%s AND session_date=%s',(ACCOUNT,day)).fetchone()
        if done and done['status']=='complete':
            summary['paper_status']='already_complete';c.commit();print(json.dumps(summary,ensure_ascii=False,default=str));return 0
        orders=c.execute("""SELECT order_id id,stock_code code,side,quantity qty,session_date session,decision_at
          FROM quant.paper_orders WHERE account_id=%s AND session_date=%s AND status='pending' ORDER BY order_id""",(ACCOUNT,day)).fetchall()
        if readiness['status']!='ready':
            c.execute("""UPDATE quant.paper_orders SET status='expired',reason='new_risk_blocked_by_input_readiness'
              WHERE account_id=%s AND session_date=%s AND status='pending' AND side='buy'""",(ACCOUNT,day))
            orders=[o for o in orders if o['side']=='sell']
        codes=sorted({o['code'] for o in orders}|{l['code'] for l in account['state']['lots']})
        try:
            calendar=c.execute("""SELECT bool_or(trade_date=%s) is_open,max(trade_date) FILTER(WHERE trade_date<%s) previous
              FROM market.trading_calendar WHERE exchange='SSE' AND is_verified AND session_status='open' AND trade_date<=%s""",(day,day,day)).fetchone()
            assert_session_continuity(account['state'].get('last_session'),calendar['previous'],calendar['is_open'])
            bars,actions=paper_bars(c,codes,day)
            result=step(account['state'],day,bars,[{**o,'decision_at':o['decision_at'].isoformat()} for o in orders],account['policy'],actions)
        except DataGap as exc:
            payload={'reason':str(exc),'readiness':summary}
            c.execute("""INSERT INTO quant.paper_sessions(account_id,session_date,status,readiness_id,payload)
              VALUES(%s,%s,'blocked',%s,%s) ON CONFLICT(account_id,session_date) DO UPDATE
              SET status='blocked',readiness_id=excluded.readiness_id,payload=excluded.payload,completed_at=clock_timestamp()""",(ACCOUNT,day,rid,jsonb(payload)))
            c.commit();print(json.dumps({'paper_status':'blocked',**payload},ensure_ascii=False,default=str));return 0
        for order in result['orders']:
            c.execute('UPDATE quant.paper_orders SET status=%s,filled_quantity=%s,reason=%s WHERE order_id=%s',
              (order['status'],order['filled_qty'],order['reason'],str(order['id'])))
        for f in result['fills']:
            c.execute("""INSERT INTO quant.paper_fills(order_id,session_date,quantity,price,fees,gross)
              VALUES(%s,%s,%s,%s,%s,%s)""",(f['order_id'],day,f['qty'],f['price'],f['fees'],f['gross']))
        c.execute('UPDATE quant.paper_accounts SET state=%s WHERE account_id=%s',(jsonb(result['state']),ACCOUNT))
        benchmark=c.execute("""SELECT b.close FROM market.benchmark_daily_v1 b JOIN market.assets a USING(asset_id)
          WHERE b.dt=%s AND a.exchange='SSE' AND a.symbol='000300' AND a.asset_type='index' AND b.quality_status='verified' LIMIT 1""",(day,)).fetchone()
        c.execute("""INSERT INTO quant.paper_nav(account_id,session_date,cash,receivable,nav,turnover,positions,benchmark_close,benchmark_name)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'沪深300价格指数')""",(ACCOUNT,day,result['cash'],result['receivable'],result['nav'],result['turnover'],jsonb(result['positions']),benchmark['close'] if benchmark else None))
        c.execute("UPDATE quant.paper_orders SET status='expired',reason='missed_session_no_retroactive_fill' WHERE account_id=%s AND session_date<%s AND status='pending'",(ACCOUNT,day))
        new_orders=0
        if readiness['status']=='ready' and day==today and (day.weekday()==4 or not account['state'].get('last_session')):
            snap=e['snapshot'];sid=snap['snapshot_id'];decision=dt.datetime.now(dt.timezone.utc)
            next_day=c.execute("""SELECT min(trade_date) d FROM market.trading_calendar WHERE exchange='SSE' AND is_verified
              AND session_status='open' AND trade_date>%s""",(decision.astimezone(TZ).date(),)).fetchone()['d']
            rows=c.execute(f"""WITH cohort AS(SELECT f.dt,
              md5(string_agg(f.asset_id::text||':'||extract(epoch FROM f.updated_at)::text,',' ORDER BY f.asset_id)) cohort_hash
              FROM factor.feature_daily f JOIN market.assets a USING(asset_id) WHERE f.dt=%s
              AND a.asset_type='stock' AND NOT coalesce(a.metadata ? 'identity_alias_of',false) GROUP BY f.dt)
              SELECT s.payload,b.close,b.amount,f.updated_at feature_updated_at,cv.cohort_hash,
              (SELECT jsonb_object_agg(factor_name,version) FROM factor.definitions
                WHERE factor_name IN('momentum_20d','reversal_5d','low_volatility_20d')) factor_versions,
              f.input_contract_version='pit-input-v1' AND f.knowledge_mode='system' AND f.input_quality->>'strict_pit_ready'='true' strict,
              l.quality_status lifecycle_quality,l.is_tradable tradable,l.is_st,l.is_suspended,i.industry_name industry,
              a.metadata->>'listing_date_verified'='true' listing_verified,
              (SELECT count(*) FROM market.trading_calendar tc WHERE tc.exchange='SSE' AND tc.is_verified
                AND tc.session_status='open' AND tc.trade_date BETWEEN a.listed_date AND f.dt) listed_sessions
              FROM analytics.research_snapshot_daily s JOIN factor.feature_daily f ON f.asset_id=s.asset_id AND f.dt=%s
              JOIN cohort cv ON cv.dt=f.dt
              JOIN market.assets a ON a.asset_id=s.asset_id
              JOIN market.canonical_bars_daily_v2 b ON b.asset_id=s.asset_id AND b.dt=f.dt AND b.quality_status='verified'
              LEFT JOIN LATERAL(SELECT * FROM market.asset_lifecycle_versions l WHERE l.asset_id=s.asset_id AND l.valid_from<=f.dt
                AND (l.valid_to IS NULL OR l.valid_to>=f.dt) AND l.available_at<=%s ORDER BY {lifecycle_order('l')} LIMIT 1)l ON true
              LEFT JOIN analytics.asset_industry_current i ON i.asset_id=s.asset_id
              WHERE s.snapshot_id=%s AND f.available_at<=%s""",(day,day,decision,sid,decision)).fetchall()
            choices=targets(rows)
            if next_day and len(choices)>=10:
                wanted={r['code']:int(dec(result['nav'])*dec(r['weight'])/dec(r['close']))//100*100 for r in choices}
                for code in sorted(set(wanted)|set(result['positions'])):
                    delta=wanted.get(code,0)-result['positions'].get(code,0)
                    if not delta:continue
                    side='buy' if delta>0 else 'sell';oid=hashlib.sha256(f'{ACCOUNT}:{sid}:{code}:{side}'.encode()).hexdigest()
                    inserted=c.execute("""INSERT INTO quant.paper_orders(order_id,account_id,snapshot_id,decision_at,session_date,stock_code,side,quantity)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",(oid,ACCOUNT,sid,decision,next_day,code,side,abs(delta)))
                    new_orders+=inserted.rowcount
            else:summary['signal_status']='insufficient_verified_diversified_targets_or_calendar'
        summary.update(paper_status='complete',new_paper_orders=new_orders,filled_orders=len(result['fills']),nav=result['nav'],cost_basis=account['policy']['cost_basis'])
        c.execute("""INSERT INTO quant.paper_sessions(account_id,session_date,status,readiness_id,payload)
           VALUES(%s,%s,'complete',%s,%s) ON CONFLICT(account_id,session_date) DO UPDATE SET
           status='complete',readiness_id=excluded.readiness_id,payload=excluded.payload,completed_at=clock_timestamp()""",(ACCOUNT,day,rid,jsonb(summary)))
        c.commit();print(json.dumps(summary,ensure_ascii=False,default=str));return 0
if __name__=='__main__':raise SystemExit(main())
