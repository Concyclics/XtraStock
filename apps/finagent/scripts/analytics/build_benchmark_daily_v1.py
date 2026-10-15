#!/usr/bin/env python3
"""Source-backed price-return benchmarks; legacy qfq is reconciliation-only.
New historical source rows use a small dedicated raw table, avoiding rewrites of
compressed market bars. Original source payloads and hashes are retained locally.
"""
from __future__ import annotations
import argparse,datetime as dt,hashlib,json,sys
from pathlib import Path
from zoneinfo import ZoneInfo
from common import connect
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts/pipeline'))
from market_kline_sina_backfill import fetch_sina_daily,secid_to_sina
from market_gap_repair import validate_rows
TZ=ZoneInfo('Asia/Shanghai')
BENCHMARKS={'1.000001':'上证指数','1.000300':'沪深300','1.000905':'中证500','1.000852':'中证1000','1.000016':'上证50','0.399001':'深证成指','0.399006':'创业板指'}
SCHEMA="""
CREATE TABLE IF NOT EXISTS market.benchmark_source_bars_v1(
 asset_id uuid NOT NULL REFERENCES market.assets(asset_id),dt date NOT NULL,source text NOT NULL,
 open double precision NOT NULL,high double precision NOT NULL,low double precision NOT NULL,close double precision NOT NULL,
 volume double precision,available_at timestamptz NOT NULL,source_sha256 text NOT NULL,
 PRIMARY KEY(asset_id,dt,source));
CREATE TABLE IF NOT EXISTS market.benchmark_daily_v1(
 asset_id uuid NOT NULL REFERENCES market.assets(asset_id),dt date NOT NULL,
 close double precision NOT NULL,daily_return double precision,price_basis text NOT NULL,
 source text NOT NULL,available_at timestamptz NOT NULL,quality_status text NOT NULL,evidence jsonb NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(asset_id,dt));
GRANT SELECT,INSERT,UPDATE ON market.benchmark_source_bars_v1,market.benchmark_daily_v1 TO finance_app;
GRANT SELECT ON market.benchmark_source_bars_v1,market.benchmark_daily_v1 TO finance_ai_reader;
"""
RAW_CTE="""WITH raw AS (
 SELECT b.asset_id,(ts AT TIME ZONE 'Asia/Shanghai')::date dt,b.open,b.high,b.low,b.close,b.source,
 greatest(b.updated_at,b.created_at) available_at
 FROM market.bars_daily b WHERE asset_id=ANY(%(ids)s) AND source IN('eastmoney:kline:day:none','sina:kline:day:none')
  AND ts>=(%(since)s::date::timestamp AT TIME ZONE 'Asia/Shanghai') AND ts<((%(until)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
 UNION ALL
 SELECT asset_id,dt,open,high,low,close,source,available_at FROM market.benchmark_source_bars_v1
 WHERE asset_id=ANY(%(ids)s) AND dt BETWEEN %(since)s AND %(until)s),
 valid AS(SELECT * FROM raw WHERE close>0 AND open>0 AND high>=greatest(open,close) AND low<=least(open,close) AND low>0)
"""

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output-dir',type=Path,required=True);ap.add_argument('--since',default='2020-01-01');ap.add_argument('--until',default=dt.datetime.now(TZ).date().isoformat());ap.add_argument('--fetch-history',action='store_true');args=ap.parse_args()
 args.output_dir.mkdir(parents=True,exist_ok=True);results=[]
 with connect(autocommit=False) as c:
  c.execute('SET TRANSACTION READ ONLY');c.execute("SET LOCAL statement_timeout='30s'")
  assets=c.execute("SELECT asset_id,name,metadata->>'eastmoney_secid' secid FROM market.assets WHERE asset_type='index' AND NOT(metadata ? 'identity_alias_of') AND metadata->>'eastmoney_secid'=ANY(%s)",(list(BENCHMARKS),)).fetchall()
 if len(assets)!=len(BENCHMARKS) or any(a['name']!=BENCHMARKS[a['secid']] for a in assets):raise RuntimeError('named benchmark security identity mismatch')
 with connect(autocommit=False,admin=True) as c:c.execute(SCHEMA);c.commit()
 if args.fetch_history:
  for a in assets:
   rows=fetch_sina_daily(secid_to_sina(a['secid']),1023,0)
   if not isinstance(rows,list):raise RuntimeError('invalid benchmark response '+a['secid'])
   received_at=dt.datetime.now(dt.timezone.utc);payload=json.dumps(rows,ensure_ascii=False,sort_keys=True);digest=hashlib.sha256(payload.encode()).hexdigest()
   rawfile=args.output_dir/f"benchmark-source-{a['secid']}-{received_at.strftime('%Y%m%dT%H%M%SZ')}.json";rawfile.write_text(payload)
   required={r['day'] for r in rows if args.since<=r.get('day','')<=args.until};selected=validate_rows(rows,required)
   if not selected:raise RuntimeError('empty benchmark history '+a['secid'])
   with connect(autocommit=False) as c:
    c.execute("SET LOCAL statement_timeout='60s'");c.execute('CREATE TEMP TABLE benchmark_ingest(LIKE market.benchmark_source_bars_v1) ON COMMIT DROP')
    with c.cursor().copy('COPY benchmark_ingest(asset_id,dt,source,open,high,low,close,volume,available_at,source_sha256) FROM STDIN') as cp:
     for day,r in selected.items():cp.write_row((a['asset_id'],day,'sina:kline:day:none',r['open'],r['high'],r['low'],r['close'],r['volume'],received_at,digest))
    inserted=c.execute('INSERT INTO market.benchmark_source_bars_v1 SELECT * FROM benchmark_ingest ON CONFLICT(asset_id,dt,source) DO NOTHING').rowcount;c.commit()
   result={'secid':a['secid'],'name':a['name'],'received':len(rows),'selected':len(selected),'inserted':inserted,'first':min(selected),'last':max(selected),'source':'sina:kline:day:none','payload_sha256':digest,'payload_file':str(rawfile)};results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
   (args.output_dir/'benchmark-fetch.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
 ids=[a['asset_id'] for a in assets];params={'ids':ids,'since':args.since,'until':args.until}
 with connect(autocommit=False) as c:
  c.execute("SET LOCAL statement_timeout='90s'")
  legacy=c.execute(RAW_CTE+""",qfq AS(SELECT asset_id,(ts AT TIME ZONE 'Asia/Shanghai')::date dt,close FROM market.bars_daily
   WHERE asset_id=ANY(%(ids)s) AND ts>=(%(since)s::date::timestamp AT TIME ZONE 'Asia/Shanghai') AND ts<((%(until)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai') AND source='eastmoney:kline:day:qfq')
   SELECT a.symbol,a.name,n.source,count(*) AS overlap_rows,max(abs(n.close-q.close)/nullif(q.close,0)) max_relative_difference
   FROM valid n JOIN qfq q USING(asset_id,dt) JOIN market.assets a USING(asset_id) GROUP BY 1,2,3 ORDER BY 1,3""",params).fetchall()
  count=c.execute(RAW_CTE+""",ranked AS(SELECT *,row_number() OVER(PARTITION BY asset_id,dt ORDER BY
    CASE source WHEN 'eastmoney:kline:day:none' THEN 1 ELSE 2 END,available_at DESC) rn FROM valid),
   stats AS(SELECT asset_id,dt,count(DISTINCT source) sources,min(close) lo,max(close) hi,max(available_at) checked_at FROM ranked GROUP BY 1,2),
   chosen AS(SELECT r.*,s.sources,(s.hi-s.lo)/nullif(s.lo,0) relative_diff,s.checked_at,
    lag(r.close) OVER(PARTITION BY r.asset_id ORDER BY r.dt) previous_close,lag(r.dt) OVER(PARTITION BY r.asset_id ORDER BY r.dt) previous_dt
    FROM ranked r JOIN stats s USING(asset_id,dt) WHERE r.rn=1),
   calendar AS(SELECT trade_date,lag(trade_date) OVER(ORDER BY trade_date) previous_session FROM market.trading_calendar WHERE exchange='SSE' AND session_status='open' AND is_verified)
   INSERT INTO market.benchmark_daily_v1(asset_id,dt,close,daily_return,price_basis,source,available_at,quality_status,evidence)
   SELECT asset_id,dt,close,CASE WHEN previous_dt=cal.previous_session THEN close/nullif(previous_close,0)-1 END,
    'index_price_return',source,checked_at,
    CASE WHEN relative_diff>0.002 THEN 'conflict' WHEN sources>=2 THEN 'verified' ELSE 'unverified' END,
    jsonb_build_object('source_count',sources,'relative_close_difference',relative_diff,'previous_date',previous_dt,
     'previous_calendar_session',cal.previous_session,'no_dividend_reinvestment',true,'legacy_qfq_excluded',true,'single_source_history_is_unverified',true)
   FROM chosen LEFT JOIN calendar cal ON cal.trade_date=dt
   ON CONFLICT(asset_id,dt) DO UPDATE SET close=excluded.close,daily_return=excluded.daily_return,
    source=excluded.source,available_at=excluded.available_at,quality_status=excluded.quality_status,evidence=excluded.evidence,updated_at=now()""",params).rowcount;c.commit()
  summary=c.execute("SELECT a.name,a.symbol,b.quality_status,count(*) rows,min(dt),max(dt),count(daily_return) returns FROM market.benchmark_daily_v1 b JOIN market.assets a USING(asset_id) GROUP BY 1,2,3 ORDER BY 1,3").fetchall()
 report={'rows_written':count,'at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'basis':'Index price return; no dividend reinvestment. Raw qfq is audit-only and excluded.','legacy_reconciliation':legacy,'summary':summary}
 (args.output_dir/'benchmark-summary.json').write_text(json.dumps(report,default=str,ensure_ascii=False,indent=2));print(json.dumps(report,default=str,ensure_ascii=False));return 0
if __name__=='__main__':raise SystemExit(main())
