"""Refresh observed EPS with the provider's explicit fiscal year, never backdate it.

Rolling API fields may change after report publication. These observations are
usable from fetch time only; historical replay must use dated document text.
Responses are cached and hashed, and retries cannot rewrite ledger evidence.
"""
from __future__ import annotations
import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
import json
from pathlib import Path
import time
import requests
try:
    from .common import connect, jsonb
except ImportError:
    from common import connect, jsonb

URL = 'https://reportapi.eastmoney.com/report/list'

def observations(payload, observed_at, raw_digest):
    year = payload.get('currentYear')
    if isinstance(year, bool) or not str(year).isdigit() or not 2000 <= int(year) <= 2100:
        raise ValueError('missing explicit API currentYear')
    result = []
    for row in payload.get('data', []):
        code, ident = row.get('stockCode', ''), row.get('infoCode', '')
        if not __import__('re').fullmatch(r'\d{6}', code) or not __import__('re').fullmatch(r'AP\d+', ident):
            raise ValueError('ambiguous source security/report identity')
        for offset, stem in enumerate(('ThisYear', 'NextYear', 'NextTwoYear')):
            for field, suffix, unit in (('eps', 'Eps', 'CNY/share'), ('pe', 'Pe', 'times')):
                raw = row.get('predict'+stem+suffix)
                if raw in (None, '', '--', '-'): continue
                try: value = Decimal(str(raw))
                except InvalidOperation: raise ValueError('malformed forecast value') from None
                if not value.is_finite(): raise ValueError('non-finite forecast value')
                # Zero and negative EPS are preserved, never promoted to a positive estimate.
                identity = [ident, int(year)+offset, field, str(value), observed_at, raw_digest]
                result.append({'observation_id': hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
                    'stock_code': code, 'institution': row.get('orgSName') or row.get('orgName') or '',
                    'source_identifier': ident, 'report_date': row['publishDate'][:10],
                    'forecast_year': int(year)+offset, 'metric': field, 'value': str(value), 'unit': unit,
                    'source_basis': 'observed_api_current_year', 'known_at': observed_at, 'observed_at': observed_at,
                    'evidence': {'source_url': URL, 'source_field': 'predict'+stem+suffix,
                        'api_current_year': int(year), 'response_sha256': raw_digest,
                        'industry': row.get('indvInduName'), 'org_code': row.get('orgCode'), 'stock_name':row.get('stockName'),
                        'availability': 'observed snapshot; not historical publication-time data'}})
    return result

def collect(output, start, end, rate=2):
    output.mkdir(parents=True, exist_ok=True)
    if not 0 < rate <= 4: raise ValueError('rate must be in (0,4]')
    session = requests.Session()
    session.headers.update({'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/report/'})
    rows, seen, expected = [], set(), None
    page = 1
    while True:
        path = output / f'page-{page:04}.json.gz'
        if path.exists():
            with gzip.open(path, 'rt') as f: cached = json.load(f)
            if (cached['start'],cached['end']) != (start,end): raise ValueError('cached scope differs')
        else:
            params = dict(industryCode='*',pageSize=100,industry='*',rating='',ratingChange='',beginTime=start,
                endTime=end,pageNo=page,fields='',qType=0,orgCode='',code='*',rcode='')
            for attempt in range(4):
                try:
                    response = session.get(URL, params=params, timeout=(10,30))
                    response.raise_for_status(); body = response.json(); break
                except (requests.RequestException, ValueError):
                    if attempt == 3: raise
                    time.sleep(2**attempt)
            cached = {'start':start,'end':end,'observed_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'response':body, 'response_sha256':hashlib.sha256(response.content).hexdigest()}
            with gzip.open(path.with_suffix('.tmp'),'wt') as f: json.dump(cached,f,ensure_ascii=False)
            path.with_suffix('.tmp').replace(path)
            time.sleep(1/rate)
        body = cached['response']
        if expected is None: expected = int(body['hits'])
        if int(body['hits']) != expected: raise ValueError('listing changed during pagination; keep cache and review')
        for item in body.get('data',[]):
            ident = item['infoCode']
            if ident in seen or not start <= item['publishDate'][:10] <= end:
                raise ValueError('duplicate or out-of-scope listing')
            seen.add(ident)
        rows.extend(observations(body,cached['observed_at'],cached['response_sha256']))
        if page % 10 == 0: print(json.dumps({'pages':page,'reports':len(seen),'expected':expected}),flush=True)
        if len(body.get('data',[])) < 100: break
        page += 1
        if page > 500: raise ValueError('bounded listing page limit exceeded')
    if len(seen) != expected: raise ValueError('incomplete listing')
    return rows, {'pages':page,'reports':len(seen),'observations':len(rows),'start':start,'end':end}

def store(conn, rows):
    docs = conn.execute("SELECT document_id,eastmoney_infocode,stock_code,institution FROM research.reports_resolved WHERE eastmoney_infocode=ANY(%s)",
        (list({r['source_identifier'] for r in rows}),)).fetchall()
    mapping = {r['eastmoney_infocode']:r for r in docs}
    if len(mapping) != len(docs): raise ValueError('nonunique report identity')
    assets=conn.execute("SELECT symbol,name FROM market.assets WHERE asset_type='stock' AND NOT(metadata ? 'identity_alias_of') AND symbol=ANY(%s)",
        (list({r['stock_code'] for r in rows}),)).fetchall()
    by_symbol={r['symbol']:r for r in assets}
    if len(assets)!=len(by_symbol):raise ValueError('nonunique source security identity')
    prior=conn.execute('''SELECT DISTINCT ON(source_identifier,forecast_year,metric)
      source_identifier,forecast_year,metric,value,document_id FROM research.earnings_observations
      WHERE source_basis='observed_api_current_year' AND source_identifier=ANY(%s)
      ORDER BY source_identifier,forecast_year,metric,observed_at DESC,observation_id DESC''',
      (list({r['source_identifier'] for r in rows}),)).fetchall()
    latest={(r['source_identifier'],r['forecast_year'],r['metric']):r for r in prior}
    inserted, unchanged, skipped, source_only = 0, 0, {}, set()
    for r in rows:
        doc = mapping.get(r['source_identifier'])
        if not doc and r['stock_code'] in by_symbol:
            # The API response itself is the observed-source evidence. Keep its
            # missing corpus link explicit instead of discarding typed values.
            doc={'document_id':None,'stock_code':r['stock_code'],'institution':r['institution']}
            source_only.add(r['source_identifier'])
        if not doc or doc['stock_code'] != r['stock_code']:
            skipped[r['source_identifier']] = 'missing_security_identity' if not doc else 'security_identity_mismatch'
            continue
        previous=latest.get((r['source_identifier'],r['forecast_year'],r['metric']))
        if previous and previous['value']==Decimal(r['value']) and previous['document_id']==doc['document_id']:
            unchanged+=1;continue
        values = {**r,'document_id':doc['document_id'],'institution':doc['institution'] or r['institution'],'evidence':jsonb(r['evidence'])}
        result = conn.execute('''INSERT INTO research.earnings_observations
            (observation_id,stock_code,institution,document_id,source_identifier,report_date,forecast_year,
             metric,value,unit,source_basis,known_at,observed_at,evidence)
            VALUES(%(observation_id)s,%(stock_code)s,%(institution)s,%(document_id)s,%(source_identifier)s,%(report_date)s,
             %(forecast_year)s,%(metric)s,%(value)s,%(unit)s,%(source_basis)s,%(known_at)s,%(observed_at)s,%(evidence)s)
            ON CONFLICT(observation_id) DO NOTHING''',values)
        inserted += result.rowcount
    return {'inserted':inserted,'unchanged':unchanged,'unresolved_documents':skipped,'source_only_document_links':sorted(source_only)}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--start',required=True);p.add_argument('--end',required=True);p.add_argument('--apply',action='store_true')
    args=p.parse_args();rows,receipt=collect(args.output,args.start,args.end)
    if args.apply:
        with connect(autocommit=False) as conn:
            conn.execute("SET LOCAL statement_timeout='90s'");conn.execute("SET LOCAL lock_timeout='3s'")
            conn.execute('SELECT pg_advisory_xact_lock(704201610)')
            receipt.update(store(conn,rows))
    (args.output/'RECEIPT.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in receipt.items() if k!='unresolved_documents'},ensure_ascii=False))
    return 2 if receipt.get('unresolved_documents') else 0

if __name__=='__main__': raise SystemExit(main())
