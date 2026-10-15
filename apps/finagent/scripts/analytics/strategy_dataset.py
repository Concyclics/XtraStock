"""Freeze a broad, deterministic, read-only research dataset. Never place orders.

This deliberately does not relax the production PIT reader's scope or gates.
Historical reconstruction includes single-source observations and the present
security master; limitations are written into every dataset manifest.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import time
from pathlib import Path
import numpy as np
from common import connect

SEED = 'strategy-balanced-v1'
FIELDS = ('open','high','low','close','volume','amount','total_return_factor','split_factor')
PRICE_SQL = """
WITH p AS MATERIALIZED (
 SELECT asset_id,dt,open,high,low,close,volume,amount,quality_status,source,available_at,updated_at
 FROM market.canonical_bars_daily_v2 WHERE asset_id=ANY(%s) AND dt BETWEEN %s AND %s
), f AS MATERIALIZED (
 SELECT asset_id,dt,total_return_factor,split_factor,quality_status
 FROM market.adjustment_factors_daily WHERE asset_id=ANY(%s) AND dt BETWEEN %s AND %s
)
SELECT p.*,f.total_return_factor,f.split_factor,f.quality_status AS adjustment_quality
FROM p LEFT JOIN f USING(asset_id,dt) ORDER BY p.asset_id,p.dt
"""
ACTION_SQL = """
WITH latest_documents AS (
 SELECT ca.*,row_number() OVER (PARTITION BY source_name,source_external_id
   ORDER BY revision_no DESC,available_at DESC,action_id DESC) AS document_revision
 FROM market.corporate_actions ca WHERE asset_id=ANY(%s)
)
SELECT * FROM (
 SELECT ca.*,row_number() OVER(PARTITION BY asset_id,ex_date,
   CASE WHEN action_type IN ('cash_dividend','stock_dividend','split','reverse_split')
        THEN 'distribution' ELSE action_type END
   ORDER BY announced_at DESC,available_at DESC,revision_no DESC,source_external_id DESC,action_id DESC) AS rn
 FROM latest_documents ca
 WHERE document_revision=1 AND quality_status<>'rejected' AND ex_date BETWEEN %s AND %s
) q WHERE rn=1 ORDER BY ex_date,asset_id
"""


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False))


def checksum(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()


def selected_assets(assets, count):
    def eligible(a):
        return a['exchange'] in ('SSE','SZSE') and a['symbol'].startswith(
            ('000','001','002','003','300','301','600','601','603','605','688','689'))
    chosen=sorted((a for a in assets if eligible(a)),key=lambda a:hashlib.sha256(
        f"{SEED}:{a['exchange']}:{a['symbol']}".encode()).hexdigest())[:count]
    return [{**a,'cohort':'cross_section_reserve' if i%5==0 else 'discovery'} for i,a in enumerate(chosen)]


def freeze(output, count=1200, since='2020-01-01', until='2026-10-13'):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if (output/'manifest.json').exists():
        manifest=json.loads((output/'manifest.json').read_text())
        if manifest['data_sha256']!=checksum(output/'market.npz'):raise ValueError('frozen dataset checksum mismatch')
        print(json.dumps({'status':'reused','manifest':str(output/'manifest.json')}));return manifest
    if not 100<=count<=3000:raise ValueError('sample count must be 100..3000')
    started=time.monotonic();cutoff=dt.datetime.now(dt.timezone.utc);batches=[]
    with connect(autocommit=False,attempts=1) as c:
        c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        c.execute("SET LOCAL statement_timeout='25s'");c.execute("SET LOCAL lock_timeout='1s'")
        assets=c.execute("""SELECT asset_id,symbol,name,exchange,industry_name,listed_date,delisted_date,lifecycle_status
          FROM market.assets WHERE asset_type='stock' AND NOT(metadata ? 'identity_alias_of') ORDER BY exchange,symbol""").fetchall()
        selected=selected_assets(assets,count)
        if len({a['asset_id'] for a in selected})!=count:raise ValueError('insufficient or duplicate security identities')
        calendar=c.execute("""SELECT trade_date,is_verified,source_name FROM market.trading_calendar
          WHERE exchange='SSE' AND session_status='open' AND trade_date BETWEEN %s AND %s
          AND (is_verified OR source_name='observed market bars fallback') ORDER BY trade_date""",(since,until)).fetchall()
        dates=[str(r['trade_date']) for r in calendar];date_index={v:i for i,v in enumerate(dates)}
        ids=[a['asset_id'] for a in selected];asset_index={a:i for i,a in enumerate(ids)}
        shape=(len(dates),count)
        data={k:np.full(shape,np.nan) for k in FIELDS}
        data['quality']=np.zeros(shape,dtype=np.int8);data['adjustment_quality']=np.zeros(shape,dtype=np.int8)
        sources={};outside=[];rows_total=0
        for start in range(0,count,16):
            if time.monotonic()-started>600:raise TimeoutError('bounded dataset read exceeded 600 seconds')
            batch=ids[start:start+16];begin=time.monotonic()
            rows=c.execute(PRICE_SQL,(batch,since,until,batch,since,until)).fetchall()
            for r in rows:
                date=str(r['dt']);j=asset_index[r['asset_id']]
                if date not in date_index:
                    outside.append({'symbol':selected[j]['symbol'],'date':date});continue
                i=date_index[date]
                if data['quality'][i,j]!=0:raise ValueError('duplicate security/date')
                for k in FIELDS:data[k][i,j]=float(r[k]) if r[k] is not None else np.nan
                data['quality'][i,j]={'verified':1,'unverified':2}.get(r['quality_status'],3)
                data['adjustment_quality'][i,j]={'verified':1,'unverified':2}.get(r['adjustment_quality'],3)
                sources[r['source']]=sources.get(r['source'],0)+1
            rows_total+=len(rows)
            batches.append({'first_index':start,'securities':len(batch),'rows':len(rows),'seconds':round(time.monotonic()-begin,3)})
            if start%128==0:print(json.dumps({'phase':'read_prices','securities_read':min(start+16,count),'total':count,'rows':rows_total}),flush=True)
        actions=[]
        for start in range(0,count,200):actions.extend(c.execute(ACTION_SQL,(ids[start:start+200],since,until)).fetchall())
        benchmarks=c.execute("""SELECT a.symbol,a.name,a.exchange,b.dt,b.close,b.price_basis,b.quality_status,b.source
          FROM market.benchmark_daily_v1 b JOIN market.assets a USING(asset_id)
          WHERE b.dt BETWEEN %s AND %s ORDER BY a.symbol,b.dt""",(since,until)).fetchall()
        c.rollback()
    data['dates']=np.asarray(dates);data['symbols']=np.asarray([a['symbol'] for a in selected])
    data['cohort']=np.asarray([a['cohort'] for a in selected])
    np.savez_compressed(output/'market.npz',**data)
    dump(output/'assets.json',selected);dump(output/'source-master.json',assets)
    dump(output/'actions.json',actions);dump(output/'benchmarks.json',benchmarks);dump(output/'calendar.json',calendar)
    present=np.sum(data['quality']>0,axis=0)
    manifest={'version':'broad-research-v1','cutoff':cutoff.isoformat(),'since':since,'until':until,'seed':SEED,
        'securities':count,'sessions':len(dates),'source_rows':rows_total,'securities_with_bars':int(np.sum(present>0)),
        'securities_without_bars':[a['symbol'] for a,n in zip(selected,present) if not n],
        'coverage_distribution':{str(q):float(np.quantile(present,q)) for q in (0,.25,.5,.75,1)},
        'bar_quality':{str(v):int(np.sum(data['quality']==v)) for v in (0,1,2,3)},
        'adjustment_quality':{str(v):int(np.sum((data['adjustment_quality']==v)&(data['quality']>0))) for v in (0,1,2,3)},
        'quality_code':{'0':'missing','1':'verified','2':'single_source_or_unverified','3':'invalid_or_incomplete'},
        'source_counts':sources,'outside_calendar':outside,'actions':len(actions),'batches':batches,
        'seconds':round(time.monotonic()-started,3),'snapshot':'single_repeatable_read_read_only_transaction',
        'data_sha256':checksum(output/'market.npz'),
        'supporting_sha256':{f:checksum(output/f) for f in ('assets.json','actions.json','benchmarks.json','calendar.json','source-master.json')},
        'limitations':['Present-day master includes retired symbols, but lifecycle flags and delisting dates are unreliable; historical membership completeness is unverified. No retrospective survivor filtering was applied.',
            'Historical source availability is reconstructed, not historical system PIT.',
            'Most bars are single-source; historical ST status and exact daily limit prices are incomplete.',
            'Index benchmarks are price returns, not dividend-reinvested total returns.'],
        'production_approved':False}
    dump(output/'manifest.json',manifest)
    (output/'queries.sql').write_text(PRICE_SQL+';\n'+ACTION_SQL+';\n')
    print(json.dumps({k:manifest[k] for k in ('securities','sessions','source_rows','securities_with_bars','seconds')},ensure_ascii=False),flush=True)
    return manifest


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--output',required=True);ap.add_argument('--count',type=int,default=1200)
    ap.add_argument('--since',default='2020-01-01');ap.add_argument('--until',default='2026-10-13')
    args=ap.parse_args();freeze(args.output,args.count,args.since,args.until)
