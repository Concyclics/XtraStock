#!/usr/bin/env python3
"""Resumable, bounded historical *unadjusted* daily-bar repair from the existing Sina source.

SQLite is the durable local work ledger; PostgreSQL retains immutable pre-existing
raw rows. A returned subset is partial, never a completed requested interval.
"""
from __future__ import annotations
import argparse,concurrent.futures,datetime as dt,fcntl,json,math,signal,sqlite3,time,sys
from pathlib import Path
from zoneinfo import ZoneInfo
import psycopg
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from scripts.pipeline.common import pg_kwargs
from market_kline_sina_backfill import fetch_sina_daily,secid_to_sina
TZ=ZoneInfo('Asia/Shanghai');SOURCE='sina:kline:day:none'

def pg():return psycopg.connect(**pg_kwargs(),connect_timeout=5)

def ledger(path):
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row;c.execute('PRAGMA journal_mode=WAL')
    c.execute('''CREATE TABLE IF NOT EXISTS jobs(asset_id TEXT PRIMARY KEY,secid TEXT NOT NULL,dates TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,ready_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL,last_error TEXT,received INTEGER NOT NULL DEFAULT 0,written INTEGER NOT NULL DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,asset_id TEXT,started_at REAL,
        finished_at REAL,result TEXT NOT NULL)''');c.commit();return c

def initialize(c,since,until):
    if c.execute('SELECT count(*) FROM jobs').fetchone()[0]:raise RuntimeError('ledger already initialized; resume without --initialize')
    with pg() as p:
        p.execute('SET TRANSACTION READ ONLY');p.execute("SET LOCAL statement_timeout='120s'")
        deficient=[r['trade_date'] for r in p.execute('''SELECT cal.trade_date FROM market.trading_calendar cal
            LEFT JOIN (SELECT dt,count(*) n FROM market.canonical_bars_daily_v2 b JOIN market.assets a USING(asset_id)
                WHERE b.dt BETWEEN %s AND %s AND a.asset_type='stock' AND NOT(a.metadata ? 'identity_alias_of') GROUP BY dt) b ON b.dt=cal.trade_date
            WHERE cal.exchange='SSE' AND cal.session_status='open' AND cal.is_verified
                AND cal.trade_date BETWEEN %s AND %s AND coalesce(n,0)<1000 ORDER BY cal.trade_date''',(since,until,since,until))]
        if not deficient:return {'deficient_dates':[],'jobs':0}
        rows=p.execute('''WITH candidates AS (
            SELECT a.asset_id,a.metadata->>'eastmoney_secid' secid,a.listed_date,
                (SELECT min(b.dt) FROM market.canonical_bars_daily_v2 b WHERE b.asset_id=a.asset_id) first_bar
            FROM market.assets a WHERE a.asset_type='stock' AND a.exchange IN('SSE','SZSE','BSE')
                AND NOT(a.metadata ? 'identity_alias_of') AND a.metadata ? 'eastmoney_secid'
                AND a.metadata->>'sina_latest_bar_date'>=%s
            ), present AS MATERIALIZED (SELECT DISTINCT asset_id,(ts AT TIME ZONE 'Asia/Shanghai')::date dt FROM market.bars_daily
                WHERE ts>=(%s::date::timestamp AT TIME ZONE 'Asia/Shanghai') AND ts<((%s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
                AND source IN('eastmoney:kline:day:none','sina:kline:day:none') AND close>0), missing AS (
            SELECT a.asset_id,a.secid,d.dt FROM candidates a CROSS JOIN unnest(%s::date[]) d(dt)
            WHERE (a.listed_date IS NULL OR a.listed_date<=d.dt)
              AND a.first_bar<=d.dt
              AND NOT EXISTS(SELECT 1 FROM present b WHERE b.asset_id=a.asset_id AND b.dt=d.dt))
            SELECT asset_id,secid,array_agg(dt ORDER BY dt) dates FROM missing GROUP BY 1,2 ORDER BY secid''',
            ((dt.date.fromisoformat(until)-dt.timedelta(days=10)).isoformat(),since,until,deficient)).fetchall()
    c.executemany('INSERT INTO jobs(asset_id,secid,dates,updated_at) VALUES(?,?,?,?)',
        [(str(r['asset_id']),r['secid'],json.dumps(r['dates'],default=str),time.time()) for r in rows]);c.commit()
    return {'deficient_dates':[str(x) for x in deficient],'jobs':len(rows),'asset_days':sum(len(r['dates']) for r in rows),
        'universe':'stocks with recent observed Sina bars; unknown/delisted/new listing gaps remain explicitly outside this initial repair universe'}

def validate_rows(rows,required):
    if not isinstance(rows,list):raise ValueError('source response is not a daily-bar list (null/unavailable response)')
    selected={};errors=[]
    for row in rows:
        day=str(row.get('day',''))
        if day not in required:continue
        try:
            dt.date.fromisoformat(day)
            values={key:float(row[key]) for key in ('open','high','low','close','volume')}
            if not all(math.isfinite(v) for v in values.values()):raise ValueError('nonfinite')
            if min(values[k] for k in ('open','high','low','close'))<=0 or values['volume']<0:raise ValueError('nonpositive price or negative volume')
            if values['high']<max(values['open'],values['close'],values['low']) or values['low']>min(values['open'],values['close']):raise ValueError('invalid OHLC')
            if day in selected and selected[day]!=values:raise ValueError('conflicting duplicate day')
            selected[day]=values
        except (KeyError,ValueError,TypeError) as e:errors.append({'date':day,'error':str(e)});selected.pop(day,None)
    if errors:raise ValueError(json.dumps(errors))
    return selected

def process(job,datalen):
    started=time.time();required=json.loads(job['dates']);symbol=secid_to_sina(job['secid'])
    if not symbol:raise ValueError('unsupported exchange')
    rows=fetch_sina_daily(symbol,datalen,0);selected=validate_rows(rows,set(required));written=0
    with pg() as p:
        p.execute("SET LOCAL statement_timeout='25s'")
        identity=p.execute("SELECT asset_type,metadata->>'identity_alias_of' alias FROM market.assets WHERE asset_id=%s",(job['asset_id'],)).fetchone()
        if not identity or identity['asset_type']!='stock' or identity['alias']:raise ValueError('security identity changed; re-plan required')
        for day,r in selected.items():
            q=p.execute('''INSERT INTO market.bars_daily(asset_id,ts,open,high,low,close,volume,amount,source)
                SELECT %s,%s,%s,%s,%s,%s,%s,NULL,%s WHERE NOT EXISTS(
                    SELECT 1 FROM market.bars_daily b WHERE b.asset_id=%s AND b.ts>=%s AND b.ts<%s
                    AND b.source IN('eastmoney:kline:day:none','sina:kline:day:none') AND b.close>0)
                ON CONFLICT(asset_id,ts,source) DO NOTHING''',
                (job['asset_id'],dt.datetime.combine(dt.date.fromisoformat(day),dt.time(),TZ),r['open'],r['high'],r['low'],r['close'],r['volume'],SOURCE,
                 job['asset_id'],dt.datetime.combine(dt.date.fromisoformat(day),dt.time(),TZ),dt.datetime.combine(dt.date.fromisoformat(day)+dt.timedelta(days=1),dt.time(),TZ)))
            written+=q.rowcount
        present={str(r['dt']) for r in p.execute('''SELECT DISTINCT (ts AT TIME ZONE 'Asia/Shanghai')::date dt
            FROM market.bars_daily WHERE asset_id=%s AND ts>=%s AND ts<%s
             AND source IN('eastmoney:kline:day:none','sina:kline:day:none') AND close>0''',
             (job['asset_id'],dt.datetime.combine(dt.date.fromisoformat(min(required)),dt.time(),TZ),dt.datetime.combine(dt.date.fromisoformat(max(required))+dt.timedelta(days=1),dt.time(),TZ)))}
        p.commit()
    missing=sorted(set(required)-present)
    return {'secid':job['secid'],'asset_id':job['asset_id'],'started_at':started,'finished_at':time.time(),
        'source':SOURCE,'requested_dates':required,'returned_dates':sorted(selected),'missing_dates':missing,
        'received':len(selected),'written':written,'status':'partial' if missing else 'done',
        'source_rows_received':len(rows),'source_first_date':rows[0].get('day') if rows else None,
        'source_last_date':rows[-1].get('day') if rows else None,
        'source_updated_at_policy':'newly recovered rows recorded now; no fabricated historical system availability',
        'last_error':'source returned no complete requested interval; possible suspension or unavailable source' if missing else None}

def status(c):
    return {'jobs':[dict(r) for r in c.execute('SELECT status,count(*) jobs,sum(received) received,sum(written) written FROM jobs GROUP BY status')],
        'attempts':c.execute('SELECT count(*) FROM attempts').fetchone()[0],'at_utc':dt.datetime.now(dt.timezone.utc).isoformat()}

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--state-dir',type=Path,required=True);ap.add_argument('--initialize',action='store_true');ap.add_argument('--status-only',action='store_true');ap.add_argument('--require-complete',action='store_true')
    ap.add_argument('--since',default='2026-08-01');ap.add_argument('--until',default='2026-10-13');ap.add_argument('--workers',type=int,default=4)
    ap.add_argument('--max-tasks',type=int,default=0);ap.add_argument('--max-seconds',type=int,default=7200);ap.add_argument('--datalen',type=int,default=100);ap.add_argument('--max-attempts',type=int,default=3)
    args=ap.parse_args()
    if args.status_only:
        path=args.state_dir/'queue.sqlite3'
        if not path.exists():raise SystemExit('queue not initialized')
        c=sqlite3.connect(f'file:{path}?mode=ro',uri=True);c.row_factory=sqlite3.Row
        report=status(c);report['all_jobs_terminal']=bool(report['jobs']) and all(r['status'] in {'done','exhausted'} for r in report['jobs']);report['all_requested_jobs_done']=bool(report['jobs']) and all(r['status']=='done' for r in report['jobs'])
        report['unfinished_jobs']=[dict(r) for r in c.execute("SELECT secid,status,attempts,last_error FROM jobs WHERE status<>'done' ORDER BY status,secid LIMIT 30")]
        print(json.dumps(report,ensure_ascii=False));return 2 if args.require_complete and not report['all_requested_jobs_done'] else 0
    args.state_dir.mkdir(parents=True,exist_ok=True)
    lock=(args.state_dir/'worker.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);c=ledger(args.state_dir/'queue.sqlite3')
    if args.initialize:
        initial=initialize(c,args.since,args.until);(args.state_dir/'initialization.json').write_text(json.dumps(initial,indent=2));print(json.dumps(initial),flush=True)
    c.execute("UPDATE jobs SET status='pending' WHERE status='running'");c.commit()
    started=time.monotonic();stop=False;done=0;consecutive_errors=0
    def handle(*_):
        nonlocal stop;stop=True
    signal.signal(signal.SIGTERM,handle);signal.signal(signal.SIGINT,handle)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(4,args.workers))) as pool:
        active={}
        while not stop or active:
            remaining=(not args.max_tasks or done+len(active)<args.max_tasks) and time.monotonic()-started<args.max_seconds
            if not stop and remaining:
                for _ in range(max(0,min(4,args.workers)-len(active))):
                    if args.max_tasks and done+len(active)>=args.max_tasks:break
                    row=c.execute("SELECT * FROM jobs WHERE status IN('pending','failed','partial') AND attempts<? AND ready_at<=? ORDER BY attempts,secid LIMIT 1",(args.max_attempts,time.time())).fetchone()
                    if not row:break
                    job=dict(row);c.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE asset_id=?",(time.time(),job['asset_id']));c.commit()
                    active[pool.submit(process,job,args.datalen)]=job
            if not active:
                waiting=c.execute("SELECT min(ready_at) FROM jobs WHERE status IN('pending','failed','partial') AND attempts<?",(args.max_attempts,)).fetchone()[0]
                if not stop and remaining and waiting is not None:
                    time.sleep(min(5,max(.1,waiting-time.time())));continue
                break
            completed,_=concurrent.futures.wait(active,timeout=1,return_when=concurrent.futures.FIRST_COMPLETED)
            for future in completed:
                job=active.pop(future)
                try:result=future.result();consecutive_errors=0
                except Exception as e:
                    result={'asset_id':job['asset_id'],'secid':job['secid'],'status':'failed','received':0,'written':0,'last_error':type(e).__name__+': '+str(e),'finished_at':time.time()};consecutive_errors+=1
                attempts=job['attempts']+1
                if result['status']!='done' and attempts>=args.max_attempts:result['status']='exhausted'
                c.execute('UPDATE jobs SET status=?,received=received+?,written=written+?,last_error=?,ready_at=?,updated_at=? WHERE asset_id=?',
                    (result['status'],result['received'],result['written'],result.get('last_error'),time.time()+300*attempts,time.time(),job['asset_id']))
                c.execute('INSERT INTO attempts(asset_id,started_at,finished_at,result) VALUES(?,?,?,?)',(job['asset_id'],result.get('started_at'),time.time(),json.dumps(result)));c.commit()
                with (args.state_dir/'attempts.jsonl').open('a') as f:f.write(json.dumps(result,ensure_ascii=False)+'\n')
                done+=1
                progress=status(c);progress['processed_this_run']=done;(args.state_dir/'progress.json').write_text(json.dumps(progress,indent=2));print(json.dumps(progress),flush=True)
                if consecutive_errors>=8:stop=True;print('STOP source failure budget reached; ledger retained',flush=True)
            if not remaining:stop=True
    final=status(c);final['all_requested_jobs_done']=all(r['status']=='done' for r in final['jobs']);final['stopped']=stop
    (args.state_dir/'progress.json').write_text(json.dumps(final,indent=2));print(json.dumps(final),flush=True)
    return 1 if consecutive_errors>=8 else 0
if __name__=='__main__':raise SystemExit(main())
