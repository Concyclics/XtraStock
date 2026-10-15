#!/usr/bin/env python3
"""Import verified listing dates from saved official exchange JSON evidence.
A CSV manifest is a request list, not evidence: each row must match its SHA-bound
saved exchange payload. Listing dates do not certify historical tradability/ST.
"""
from __future__ import annotations
import argparse,csv,datetime as dt,hashlib,json,re
from pathlib import Path
from urllib.parse import urlparse
import sys
ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.analytics.common import connect
from psycopg.types.json import Jsonb
FORMATS={'sse_main':('SSE','SECURITY_CODE_A','SECURITY_ABBR_A','LISTING_DATE'),
         'sse_star':('SSE','A_STOCK_CODE','COMPANY_ABBR','LIST_DATE'),
         'szse_json':('SZSE','agdm','agjc','agssrq')}

def normalized_date(value):
    text=str(value).strip().replace('/','-')
    if re.fullmatch(r'\d{8}',text):text=text[:4]+'-'+text[4:6]+'-'+text[6:]
    d=dt.date.fromisoformat(text)
    if d<dt.date(1980,1,1) or d>dt.date.today()+dt.timedelta(days=366):raise ValueError('listing date outside credible range')
    return d.isoformat()

def parsed_source(path,kind,url,observed_at,expected_sha=None):
    exchange,code_key,name_key,date_key=FORMATS[kind];host=urlparse(url).hostname or ''
    domain='sse.com.cn' if exchange=='SSE' else 'szse.cn'
    if urlparse(url).scheme!='https' or not(host==domain or host.endswith('.'+domain)):raise ValueError('evidence URL is not the matching official exchange HTTPS domain')
    observed=dt.datetime.fromisoformat(observed_at)
    if observed.tzinfo is None:raise ValueError('observed_at requires a timezone')
    if observed>dt.datetime.now(dt.timezone.utc)+dt.timedelta(minutes=5):raise ValueError('observed_at cannot be in the future')
    raw=path.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    if expected_sha and digest!=expected_sha:raise ValueError('source file hash mismatch')
    body=json.loads(raw);records=[]
    if exchange=='SSE':records=body.get('result') or (body.get('pageHelp') or {}).get('data') or []
    elif isinstance(body,list):
        for section in body:records.extend(section.get('data') or [])
    elif isinstance(body,dict):records=body.get('data') or []
    parsed=[];invalid=[]
    for r in records:
        try:
            code=str(r.get(code_key,'')).strip();name=re.sub('<[^>]+>','',str(r.get(name_key,''))).strip()
            if not re.fullmatch(r'\d{6}',code):raise ValueError('invalid security code')
            if exchange=='SSE' and not code.startswith(('600','601','603','605','688','689')):raise ValueError('not an A share code')
            if exchange=='SZSE' and not code.startswith(('000','001','002','003','300','301')):raise ValueError('not an A share code')
            date=normalized_date(r.get(date_key,''))
            parsed.append({'exchange':exchange,'symbol':code,'stock_name':name,'listed_date':date,
                'source_url':url,'source_file':str(path.resolve()),'source_sha256':digest,'source_format':kind,
                'observed_at':observed.isoformat(),'source_row':r})
        except (TypeError,ValueError) as e:invalid.append({'code':r.get(code_key),'error':str(e)})
    if not parsed:raise ValueError('no valid listing rows in source')
    keys={}
    for r in parsed:
        if r['symbol'] in keys and keys[r['symbol']]!=r['listed_date']:raise ValueError('source has conflicting listing dates')
        keys[r['symbol']]=r['listed_date']
    return parsed,invalid

def manifest_rows(path):
    rows=[];invalid=[];cache={}
    with path.open(encoding='utf-8-sig') as handle:
        items=list(csv.DictReader(handle))
    for item in items:
        required={'exchange','symbol','stock_name','listed_date','source_url','source_file','source_sha256','source_format','observed_at'}
        if not required.issubset(item):raise ValueError('CSV manifest is missing required fixed-schema columns')
        key=tuple(item[k] for k in ('source_file','source_sha256','source_format','source_url','observed_at'))
        if key not in cache:
            source,errors=parsed_source(Path(item['source_file']),item['source_format'],item['source_url'],item['observed_at'],item['source_sha256']);invalid.extend(errors);cache[key]={(r['exchange'],r['symbol'],r['listed_date']):r for r in source}
        match=cache[key].get((item['exchange'],item['symbol'],normalized_date(item['listed_date'])))
        if not match:raise ValueError('CSV row is not supported by its declared official payload: '+item['symbol'])
        rows.append(match)
    return rows,invalid

def main():
    ap=argparse.ArgumentParser();g=ap.add_mutually_exclusive_group(required=True);g.add_argument('--source-json',type=Path);g.add_argument('--csv-manifest',type=Path)
    ap.add_argument('--source-format',choices=FORMATS);ap.add_argument('--source-url');ap.add_argument('--observed-at');ap.add_argument('--source-sha256');ap.add_argument('--output-dir',type=Path,required=True);ap.add_argument('--apply',action='store_true');args=ap.parse_args()
    if args.source_json:
        if not all((args.source_format,args.source_url,args.observed_at)):ap.error('JSON import requires format, official URL and observed timestamp')
        rows,invalid=parsed_source(args.source_json,args.source_format,args.source_url,args.observed_at,args.source_sha256)
    else:rows,invalid=manifest_rows(args.csv_manifest)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    with connect(autocommit=False) as c:
        c.execute('SET TRANSACTION READ ONLY');c.execute("SET LOCAL statement_timeout='25s'")
        assets=c.execute("SELECT * FROM market.assets WHERE asset_type='stock' AND NOT(metadata ? 'identity_alias_of') AND symbol=ANY(%s)",([r['symbol'] for r in rows],)).fetchall()
    bykey={(a['exchange'],a['symbol']):a for a in assets};matched=[];missing=[];conflicts=[]
    for r in rows:
        a=bykey.get((r['exchange'],r['symbol']))
        if not a:missing.append({k:r[k] for k in ('symbol','exchange','stock_name')});continue
        if a['listed_date'] and str(a['listed_date'])!=r['listed_date']:
            conflicts.append({'symbol':r['symbol'],'existing':str(a['listed_date']),'official':r['listed_date']});continue
        matched.append({**r,'asset_id':a['asset_id']})
    tag=(args.source_format or 'manifest')+'-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    before=args.output_dir/(tag+'-assets-before.json');before.write_text(json.dumps(assets,default=str,ensure_ascii=False,indent=2))
    report={'parsed_rows':len(rows),'matched':len(matched),'missing_assets':missing,'conflicts':conflicts,'invalid_source_rows':invalid,'applied':False,
        'scope':'listing date evidence only; historical lifecycle/tradability/ST remain unverified'}
    if args.apply:
        with connect(autocommit=False,admin=True) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS market.security_master_evidence_v1(
                asset_id uuid NOT NULL REFERENCES market.assets(asset_id),source_sha256 text NOT NULL,
                listed_date date NOT NULL,source_url text NOT NULL,observed_at timestamptz NOT NULL,
                evidence jsonb NOT NULL,recorded_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(asset_id,source_sha256));
                GRANT SELECT,INSERT ON market.security_master_evidence_v1 TO finance_app;
                GRANT SELECT ON market.security_master_evidence_v1 TO finance_ai_reader""");c.commit()
        with connect(autocommit=False) as c:
            c.execute("SET LOCAL statement_timeout='60s'")
            c.execute('CREATE TEMP TABLE security_master_ingest(asset_id uuid,listed_date date,source_sha256 text,source_url text,observed_at timestamptz,evidence jsonb) ON COMMIT DROP')
            with c.cursor().copy('COPY security_master_ingest FROM STDIN') as cp:
                for r in matched:cp.write_row((r['asset_id'],r['listed_date'],r['source_sha256'],r['source_url'],r['observed_at'],json.dumps({k:v for k,v in r.items() if k!='asset_id'},ensure_ascii=False)))
            c.execute("""INSERT INTO market.security_master_evidence_v1(asset_id,listed_date,source_sha256,source_url,observed_at,evidence)
                SELECT asset_id,listed_date,source_sha256,source_url,observed_at,evidence FROM security_master_ingest
                ON CONFLICT(asset_id,source_sha256) DO NOTHING""")
            changed=c.execute("""UPDATE market.assets a SET listed_date=s.listed_date,
                metadata=a.metadata||jsonb_build_object('listing_date_verified',true,'listing_date_source_url',s.source_url,
                    'listing_date_source_sha256',s.source_sha256,'listing_date_observed_at',s.observed_at,
                    'listing_date_verified_at',now(),'listing_date_scope','official historical listing fact observed now; not historical tradability'),updated_at=now()
                FROM security_master_ingest s WHERE a.asset_id=s.asset_id AND (a.listed_date IS NULL OR a.listed_date=s.listed_date)""").rowcount
            c.commit()
        report.update(applied=True,updated_assets=changed)
    (args.output_dir/(tag+'-import.json')).write_text(json.dumps(report,default=str,ensure_ascii=False,indent=2));print(json.dumps(report,default=str,ensure_ascii=False));return 2 if conflicts or invalid else 0
if __name__=='__main__':raise SystemExit(main())
