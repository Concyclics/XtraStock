#!/usr/bin/env python3
"""Record reviewed issuer suspension evidence without inventing daily prices.

A manifest references exact saved announcement bodies and excerpts. It may certify
only requested dates inside a closed suspension interval or an explicit single day.
The raw repair queue is never relabelled done; classifications live in a side table.
"""
from __future__ import annotations
import argparse,datetime as dt,hashlib,json,re,sys
from pathlib import Path
from urllib.parse import urlparse
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scripts.analytics.common import connect
from scripts.pipeline.common import pipeline_lock
from scripts.analytics.lifecycle_evidence import lifecycle_order
from psycopg.types.json import Jsonb
SOURCE='cninfo:verified_suspension_gap:v1'

def validate_item(item,documents):
    start=dt.date.fromisoformat(item['interval_start'])
    resume=dt.date.fromisoformat(item['resume_date']) if item.get('resume_date') else None
    confirmed=dt.date.fromisoformat(item['confirmed_through']) if item.get('confirmed_through') else None
    if resume is not None and resume<=start:raise ValueError('resume must follow suspension')
    if confirmed is not None and (confirmed<start or resume is not None):raise ValueError('invalid confirmation boundary')
    if not item.get('manual_review'):raise ValueError('reviewed evidence required')
    dates=[dt.date.fromisoformat(x) for x in item['missing_dates']]
    if not dates or len(set(dates))!=len(dates):raise ValueError('empty or duplicate date set')
    end=resume-dt.timedelta(days=1) if resume else confirmed or start
    if not all(start<=x<=end for x in dates):raise ValueError('requested date outside proven interval')
    events={};public=[]
    for source in item['sources']:
        document=documents[source['document_id']];body=document['body'];normalized=re.sub(r'\s+','',body)
        if document['stock_code']!=item['stock_code'] or item['stock_code'] not in normalized:raise ValueError('stock identity mismatch')
        if hashlib.sha256(body.encode()).hexdigest()!=source['body_sha256']:raise ValueError('source body changed')
        if not source['quote_normalized'] or source['quote_normalized'] not in normalized:raise ValueError('quote absent from source')
        host=urlparse(document['source_url'] or '').hostname or ''
        if host!='static.cninfo.com.cn' or document['source_url']!=source['source_url']:raise ValueError('official document URL mismatch')
        if dt.datetime.fromisoformat(str(document['announced_at']))!=dt.datetime.fromisoformat(source['public_available_at']):raise ValueError('public timestamp mismatch')
        event_date=dt.date.fromisoformat(source['event_date']);cn=f'{event_date.year}年{event_date.month}月{event_date.day}日'
        if source['event'] not in ('suspend','resume','confirm'):raise ValueError('unknown suspension event')
        word={'suspend':'停牌','resume':'复牌','confirm':'继续实施停牌'}[source['event']]
        if cn not in source['quote_normalized'] or word not in source['quote_normalized']:raise ValueError('event assertion lacks dated source quote')
        events[source['event']]=event_date;public.append(document['announced_at'])
    if events.get('suspend')!=start or (resume is not None and events.get('resume')!=resume):raise ValueError('start/resume evidence mismatch')
    if confirmed is not None:
        if item['method']!='confirmed_suspension_interval' or events.get('confirm')!=confirmed:raise ValueError('confirmation evidence mismatch')
    elif resume is None:
        if item['method']!='explicit_single_day' or not any(re.search(r'停牌(?:一[天日]|1[天日]|一个交易日)',s['quote_normalized']) for s in item['sources']):raise ValueError('single-day suspension not explicit')
    elif item['method']!='closed_suspension_interval':raise ValueError('unknown interval proof method')
    return max(public)

def main():
    p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    manifest=json.loads(a.manifest.read_text());items=manifest['items'];ids=list({s['document_id'] for item in items for s in item['sources']});a.output_dir.mkdir(parents=True,exist_ok=True)
    with connect(autocommit=False) as c:
        c.execute('SET TRANSACTION READ ONLY');c.execute("SET LOCAL statement_timeout='25s'")
        rows=c.execute("""SELECT f.document_id,f.stock_code,f.source_url,f.announced_at,f.available_at,
            coalesce(nullif(d.content_text,''),d.content_md,d.raw_text,'') body FROM company.filings f
            JOIN corpus.documents d ON d.id=f.document_id WHERE f.document_id=ANY(%s::uuid[])""",(ids,)).fetchall()
        documents={str(r['document_id']):r for r in rows};assets={str(r['asset_id']):r for r in c.execute(
            "SELECT asset_id,symbol,asset_type,exchange,metadata FROM market.assets WHERE asset_id=ANY(%s::uuid[])",([x['asset_id'] for x in items],))}
        for item in items:
            validate_item(item,documents);asset=assets[item['asset_id']]
            if asset['symbol']!=item['stock_code'] or asset['asset_type']!='stock' or asset['metadata'].get('identity_alias_of'):raise ValueError('canonical asset identity mismatch')
    report={'assets':len(items),'asset_days':sum(len(x['missing_dates']) for x in items),'applied':False,
            'classification':'validated_nontrading','raw_prices_inserted':0,'repair_queue_modified':False,
            'active_lifecycle_promoted':False,'manifest_sha256':hashlib.sha256(a.manifest.read_bytes()).hexdigest()}
    if a.apply:
        with pipeline_lock('analytics_core'):
            with connect(autocommit=False,admin=True) as c:
                c.execute("SET LOCAL statement_timeout='25s'");c.execute("SET LOCAL lock_timeout='5s'")
                c.execute("""CREATE TABLE IF NOT EXISTS market.nontrading_gap_evidence_v1(
                    asset_id uuid NOT NULL REFERENCES market.assets,dt date NOT NULL,evidence_sha256 text NOT NULL,
                    classification text NOT NULL CHECK(classification='validated_nontrading'),
                    public_available_at timestamptz NOT NULL,recorded_at timestamptz NOT NULL DEFAULT now(),
                    source_document_ids uuid[] NOT NULL,evidence jsonb NOT NULL,
                    PRIMARY KEY(asset_id,dt,evidence_sha256));
                    GRANT SELECT,INSERT ON market.nontrading_gap_evidence_v1 TO finance_app;
                    GRANT SELECT ON market.nontrading_gap_evidence_v1 TO finance_ai_reader""")
            with connect(autocommit=False) as c:
                c.execute("SET LOCAL statement_timeout='25s'");c.execute("SET LOCAL lock_timeout='5s'")
                c.execute("SELECT pg_advisory_xact_lock(hashtextextended('nontrading-gap-evidence-v1',0))")
                before=c.execute("SELECT * FROM market.asset_lifecycle_versions WHERE asset_id=ANY(%s::uuid[]) ORDER BY asset_id,available_at",([x['asset_id'] for x in items],)).fetchall()
                (a.output_dir/'lifecycle-before.json').write_text(json.dumps(before,default=str,ensure_ascii=False,indent=2))
                written=life_written=0
                for item in items:
                    public=validate_item(item,documents);asset=assets[item['asset_id']]
                    digest=hashlib.sha256(json.dumps(item,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                    source_ids=list(dict.fromkeys(s['document_id'] for s in item['sources']))
                    metadata={**item,'evidence_sha256':digest,'public_available_at':str(public),
                        'recorded_at_semantics':'available_at/created_at are actual import time; historical system knowledge not backdated',
                        'verified_fields':['lifecycle_status','is_suspended','is_tradable'],
                        'is_st_verified':False,'scope':'only reviewed missing market-gap dates; no resumption-active inference'}
                    for day in item['missing_dates']:
                        # The evidence and lifecycle add are one transaction. Contradictory
                        # actual price data require review instead of automatic suppression.
                        conflicting=c.execute("SELECT 1 FROM market.bars_daily WHERE asset_id=%s AND ts>=(%s::date::timestamp AT TIME ZONE 'Asia/Shanghai') AND ts<((%s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai') AND close>0 LIMIT 1",(item['asset_id'],day,day)).fetchone()
                        if conflicting:raise ValueError('price exists on claimed missing suspension day: '+item['stock_code']+' '+day)
                        written+=c.execute("""INSERT INTO market.nontrading_gap_evidence_v1(asset_id,dt,evidence_sha256,classification,public_available_at,source_document_ids,evidence)
                            VALUES(%s,%s,%s,'validated_nontrading',%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (item['asset_id'],day,digest,public,source_ids,Jsonb(metadata))).rowcount
                        # Preserve the earlier unverified ST field, explicitly excluding it
                        # from this evidence's verified scope. Suspension blocks trading either way.
                        previous=c.execute(f"SELECT is_st FROM market.asset_lifecycle_versions WHERE asset_id=%s AND valid_from<=%s AND (valid_to IS NULL OR valid_to>=%s) ORDER BY {lifecycle_order()} LIMIT 1",(item['asset_id'],day,day)).fetchone()
                        life_written+=c.execute("""INSERT INTO market.asset_lifecycle_versions(asset_id,valid_from,valid_to,lifecycle_status,is_tradable,is_st,is_suspended,exchange,source_name,source_document_id,available_at,quality_status,metadata)
                            SELECT %s,%s,%s,'suspended',false,%s,true,%s,%s,%s,now(),'verified',%s
                            WHERE NOT EXISTS(SELECT 1 FROM market.asset_lifecycle_versions WHERE asset_id=%s AND valid_from=%s AND valid_to=%s AND source_name=%s AND metadata->>'evidence_sha256'=%s)""",
                            (item['asset_id'],day,day,bool(previous and previous['is_st']),asset['exchange'],SOURCE,source_ids[0],Jsonb(metadata),item['asset_id'],day,day,SOURCE,digest)).rowcount
                c.commit();report.update(applied=True,evidence_rows_inserted=written,lifecycle_rows_inserted=life_written)
    report['at_utc']=dt.datetime.now(dt.timezone.utc).isoformat();(a.output_dir/'nontrading-import-result.json').write_text(json.dumps(report,indent=2));print(json.dumps(report));return 0
if __name__=='__main__':raise SystemExit(main())
