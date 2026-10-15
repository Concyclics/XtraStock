#!/usr/bin/env python3
"""Rebuild a single company's graph/technical factor inputs with an audit snapshot."""
import argparse,datetime as dt,json
from pathlib import Path
from common import connect
from build_temporal_graph_features_daily import SQL as GRAPH_SQL
from build_factor_features import FEATURE_SQL


def main():
 p=argparse.ArgumentParser();p.add_argument('--stock-code',required=True);p.add_argument('--audit-dir',type=Path,required=True)
 args=p.parse_args();args.audit_dir.mkdir(parents=True,exist_ok=True)
 graph=GRAPH_SQL.replace("AND a.asset_type='stock'", "AND a.asset_type='stock' AND a.symbol=%(stock_code)s",1)
 assert graph!=GRAPH_SQL
 graph=graph.replace("(%(as_of)s::date + time '23:59:59') AT TIME ZONE 'Asia/Shanghai' AS cutoff",
  "least((%(as_of)s::date + time '23:59:59') AT TIME ZONE 'Asia/Shanghai',now()) AS cutoff")
 features=FEATURE_SQL.replace('WHERE t.dt BETWEEN %(since)s AND %(until)s','WHERE t.dt BETWEEN %(since)s AND %(until)s AND t.asset_id=%(asset_id)s',1)
 assert features!=FEATURE_SQL
 features=features.replace("coalesce(t.available_at, (t.dt::timestamp + time '15:05') AT TIME ZONE 'Asia/Shanghai') AS available_at",
  "greatest(coalesce(t.available_at, (t.dt::timestamp + time '15:05') AT TIME ZONE 'Asia/Shanghai'),gf.available_at) AS available_at")
 with connect() as c:
  c.execute("SET statement_timeout='180s'")
  asset=c.execute("SELECT asset_id FROM market.assets WHERE symbol=%s AND asset_type='stock'",(args.stock_code,)).fetchone()['asset_id']
  dates=[r['dt'] for r in c.execute('SELECT dt FROM analytics.asset_technical_daily WHERE asset_id=%s ORDER BY dt',(asset,))]
  assert dates
  before={}
  for table in ('analytics.graph_feature_daily','factor.feature_daily','factor.daily_scores'):
   before[table]=c.execute(f'SELECT * FROM {table} WHERE asset_id=%s',(asset,)).fetchall()
  backup=args.audit_dir/'company-derived-before.json'
  if not backup.exists():backup.write_text(json.dumps(before,ensure_ascii=False,default=str,indent=2))
  for date in dates:
   n=c.execute(graph,{'as_of':date,'stock_code':args.stock_code}).rowcount
   print(json.dumps({'graph_date':str(date),'upserted':n}),flush=True)
  n=c.execute(features,{'since':min(dates),'until':max(dates),'asset_id':asset}).rowcount
  result={'stock_code':args.stock_code,'graph_dates':len(dates),'feature_rows':n,'first_date':str(min(dates)),'last_date':str(max(dates))}
  (args.audit_dir/'company-derived-result.json').write_text(json.dumps(result,indent=2))
  print(json.dumps(result),flush=True)
if __name__=='__main__':main()
