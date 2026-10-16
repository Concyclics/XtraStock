"""Re-extract dated, unit-verified forecasts from newly recovered DB documents.

No OCR/model/network job is launched. Extraction is resumable by document,
parser and exact body hash, with explicit empty/conflicting extraction receipts.
"""
from __future__ import annotations
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
try:
    from .common import connect,jsonb
    from .forecast_semantics import extract_dated_forecasts,PARSER
    from .forecast_research_v3 import public_date
except ImportError:
    from common import connect,jsonb
    from forecast_semantics import extract_dated_forecasts,PARSER
    from forecast_research_v3 import public_date

SQL='''SELECT r.document_id,r.stock_code,r.institution,r.report_date,r.eastmoney_infocode,
 r.source_url,d.published_at,d.content_text,d.updated_at
 FROM research.reports_resolved r JOIN corpus.documents d ON d.id=r.document_id
 WHERE r.report_type='stock' AND r.report_date BETWEEN %s AND %s
 AND (r.report_date,r.document_id)>(%s,%s::uuid) AND length(d.content_text)>80
 AND NOT EXISTS(SELECT 1 FROM research.report_forecasts f WHERE f.document_id=r.document_id
   AND f.metadata->>'year_verified'='true' AND f.metadata->>'year_basis'='explicit_dated_report_text'
   AND f.metadata->>'content_sha256'=encode(sha256(convert_to(d.content_text,'UTF8')),'hex'))
 ORDER BY r.report_date,r.document_id LIMIT 300'''

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--since',required=True);p.add_argument('--until',required=True)
    p.add_argument('--output',required=True,type=Path);p.add_argument('--apply',action='store_true');a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=True);progress=a.output/'PROGRESS.json'
    state=json.loads(progress.read_text()) if progress.exists() else {'cursor':[a.since,'00000000-0000-0000-0000-000000000000'],'counts':{},'since':a.since,'until':a.until}
    if (state['since'],state['until'])!=(a.since,a.until):raise ValueError('backfill scope changed')
    counts=Counter(state['counts'])
    while True:
        with connect(autocommit=False) as conn:
            conn.execute("SET LOCAL statement_timeout='90s'");conn.execute("SET LOCAL lock_timeout='3s'")
            if not a.apply:conn.execute('SET TRANSACTION READ ONLY')
            rows=conn.execute(SQL,(a.since,a.until,*state['cursor'])).fetchall()
            if not rows:break
            existing={(str(r['document_id']),r['content_digest']) for r in conn.execute('''SELECT document_id,content_digest
              FROM research.earnings_extraction_receipts WHERE document_id=ANY(%s) AND parser_version=%s''',
              ([r['document_id'] for r in rows],PARSER))}
            observation_batch=[];receipt_batch=[]
            for r in rows:
                text=r['content_text'];body=hashlib.sha256(text.encode()).hexdigest();now=dt.datetime.now(dt.timezone.utc)
                if (str(r['document_id']),body) in existing:counts['cached_documents']+=1;continue
                items,issues=extract_dated_forecasts(text,r['report_date'].year)
                counts['documents_parsed']+=1;counts['documents_with_eps']+=int(any(i['field']=='eps' for i in items))
                counts['documents_with_issues']+=int(bool(issues));counts['empty_documents']+=int(not items)
                for item in items:
                    counts[item['field']]+=1
                    key=hashlib.sha256(json.dumps([str(r['document_id']),PARSER,body,item['field'],item['forecast_year'],item['value']]).encode()).hexdigest()
                    evidence={'source_url':r['source_url'],'excerpt':item['evidence'],'parser':PARSER,'content_sha256':body,
                        'public_known_on':public_date(str(r['report_date']),str(r['published_at']) if r['published_at'] else None,r['eastmoney_infocode']),
                        'availability':'historical public reconstruction; system first read is observed_at'}
                    if a.apply:
                        observation_batch.append((key,r['stock_code'],r['institution'] or '',r['document_id'],r['eastmoney_infocode'] or str(r['document_id']),
                           r['report_date'],item['forecast_year'],item['field'],item['value'],item['unit'],now,now,jsonb(evidence)))
                if a.apply:receipt_batch.append((r['document_id'],PARSER,body,now,len(items),jsonb(issues)))
            if a.apply:
                with conn.cursor() as cur:
                    cur.executemany('''INSERT INTO research.earnings_observations(observation_id,stock_code,institution,document_id,
                      source_identifier,report_date,forecast_year,metric,value,unit,source_basis,known_at,observed_at,evidence)
                      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'explicit_dated_report_text',%s,%s,%s) ON CONFLICT DO NOTHING''',observation_batch)
                    counts['inserted_observations']+=cur.rowcount
                    cur.executemany('''INSERT INTO research.earnings_extraction_receipts VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',receipt_batch)
        state.update(cursor=[str(rows[-1]['report_date']),str(rows[-1]['document_id'])],counts=dict(counts),status='running')
        temp=progress.with_suffix('.tmp');temp.write_text(json.dumps(state,ensure_ascii=False,indent=2));temp.replace(progress)
        print(json.dumps(state['counts']),flush=True)
    state.update(status='complete',counts=dict(counts));progress.write_text(json.dumps(state,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
