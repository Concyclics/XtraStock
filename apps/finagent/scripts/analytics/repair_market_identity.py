#!/usr/bin/env python3
"""Plan/apply confirmed security identity aliases; retain all original raw rows."""
from __future__ import annotations
import argparse,datetime as dt,json,sys,unicodedata
from pathlib import Path
from psycopg.types.json import Jsonb
from common import connect
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'Spider/eastmoney'))
from eastmoney.pg_storage import _asset_type_from_code

def normalized_name(value):
    return ''.join(unicodedata.normalize('NFKC',value or '').split())

def target_type(secid):
    market,code=secid.split('.',1)
    if market=='0' and code.startswith(('000','001','002','003','300','301')):
        return 'stock'
    if secid in {'0.899050','0.899601','2.930050','2.932000'}:
        return 'index'
    return None

def plan(conn):
    candidates=conn.execute("""SELECT s.asset_id stock_id,s.symbol,s.name stock_name,s.exchange,
        s.metadata->>'eastmoney_secid' secid,i.asset_id index_id,i.name index_name,
        i.metadata->>'eastmoney_secid' index_secid
        FROM market.assets s JOIN market.assets i USING(symbol,exchange)
        WHERE s.asset_type='stock' AND i.asset_type='index' ORDER BY s.exchange,s.symbol""").fetchall()
    rows=[];excluded=[]
    for r in candidates:
        typ=target_type(r['secid'] or '') if r['secid'] else None
        if not typ or r['secid']!=r['index_secid'] or normalized_name(r['stock_name'])!=normalized_name(r['index_name']):
            excluded.append({**r,'reason':'unconfirmed identity or name mismatch'});continue
        rows.append({'alias_asset_id':r['index_id'] if typ=='stock' else r['stock_id'],
            'canonical_asset_id':r['stock_id'] if typ=='stock' else r['index_id'],
            'canonical_type':typ,'secid':r['secid'],'symbol':r['symbol'],'exchange':r['exchange'],
            'name':r['stock_name'],'basis':'same exchange/secid/normalized company name + explicit code family; no cross-exchange merge'})
    return rows,excluded

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--apply',action='store_true');ap.add_argument('--output-dir',type=Path,required=True);args=ap.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    with connect(autocommit=False,admin=False) as c:
        c.execute('SET TRANSACTION READ ONLY');c.execute("SET LOCAL statement_timeout='45s'");rows,excluded=plan(c)
        ids=[r['alias_asset_id'] for r in rows]+[r['canonical_asset_id'] for r in rows]
        assets=c.execute('SELECT * FROM market.assets WHERE asset_id=ANY(%s)',(ids,)).fetchall()
    stamp=dt.datetime.now(dt.timezone.utc).isoformat();manifest={'created_at':stamp,'aliases':rows,'excluded':excluded}
    (args.output_dir/'identity-plan.json').write_text(json.dumps(manifest,default=str,ensure_ascii=False,indent=2))
    backup=args.output_dir/'identity-assets-before.json'
    if not backup.exists():backup.write_text(json.dumps(assets,default=str,ensure_ascii=False,indent=2))
    if not args.apply:print(json.dumps({'confirmed':len(rows),'excluded':len(excluded),'applied':False}));return 0
    with connect(autocommit=False,admin=True) as c:
        c.execute("SET LOCAL statement_timeout='45s'")
        c.execute("""CREATE TABLE IF NOT EXISTS market.asset_identity_aliases(
            alias_asset_id uuid PRIMARY KEY REFERENCES market.assets(asset_id),
            canonical_asset_id uuid NOT NULL REFERENCES market.assets(asset_id),
            canonical_type text NOT NULL,reason text NOT NULL,evidence jsonb NOT NULL,
            corrected_at timestamptz NOT NULL DEFAULT now(),CHECK(alias_asset_id<>canonical_asset_id));
            GRANT SELECT,INSERT,UPDATE ON market.asset_identity_aliases TO finance_app;
            GRANT SELECT ON market.asset_identity_aliases TO finance_ai_reader""")
        c.commit()
    results=[]
    with connect(autocommit=False,admin=False) as c:
        c.execute("SET LOCAL statement_timeout='60s'")
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended('market-identity-repair-v1',0))")
        before=c.execute("""SELECT asset_id,count(*) rows,min(dt) first_date,max(dt) last_date
            FROM market.canonical_bars_daily_v2 WHERE asset_id=ANY(%s) GROUP BY asset_id""",(ids,)).fetchall()
        for row in rows:
            old,new=row['alias_asset_id'],row['canonical_asset_id']
            existing=c.execute('SELECT canonical_asset_id FROM market.asset_identity_aliases WHERE alias_asset_id=%s',(old,)).fetchone()
            if existing and existing['canonical_asset_id']!=new:raise RuntimeError('conflicting existing identity alias')
            c.execute("""INSERT INTO market.asset_identity_aliases(alias_asset_id,canonical_asset_id,canonical_type,reason,evidence)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(alias_asset_id) DO NOTHING""",
                (old,new,row['canonical_type'],'confirmed legacy asset_type misclassification; raw preserved',Jsonb(json.loads(json.dumps(row,default=str)))))
            c.execute("""UPDATE market.assets SET metadata=metadata||jsonb_build_object(
                'identity_alias_of',%s::text,'identity_correction','legacy asset_type; raw retained'),updated_at=now()
                WHERE asset_id=%s AND metadata->>'identity_alias_of' IS DISTINCT FROM %s""",(str(new),old,str(new)))
            results.append({**row,'mode':'alias resolution at canonical materialization','source_raw_retained':True})
        c.commit()
    (args.output_dir/'identity-canonical-before.json').write_text(json.dumps(before,default=str,ensure_ascii=False,indent=2))
    (args.output_dir/'identity-transactions.jsonl').write_text(''.join(json.dumps(r,default=str,ensure_ascii=False)+'\n' for r in results))
    summary={'confirmed':len(rows),'excluded':len(excluded),'source_raw_deleted':0,'source_raw_copied':0,
        'mode':'non-destructive alias; historical canonical rematerialization required',
        'canonical_rows_before':sum(r['rows'] for r in before),
        'completed_at':dt.datetime.now(dt.timezone.utc).isoformat()}
    (args.output_dir/'identity-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    (args.output_dir/'canonical-asset-ids.json').write_text(json.dumps([str(r['canonical_asset_id']) for r in rows]))
    print(json.dumps(summary,ensure_ascii=False));return 0
if __name__=='__main__':raise SystemExit(main())
