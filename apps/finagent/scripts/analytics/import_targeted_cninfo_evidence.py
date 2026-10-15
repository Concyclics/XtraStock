#!/usr/bin/env python3
"""Precisely import saved CNINFO announcement PDFs and extracted text; no web crawl."""
from __future__ import annotations
import argparse,datetime as dt,hashlib,html,json,re,sys
from pathlib import Path
from urllib.parse import urlparse
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scripts.analytics.common import connect
from psycopg.types.json import Jsonb

def main():
 p=argparse.ArgumentParser();p.add_argument('--api-response',type=Path,required=True);p.add_argument('--manifest',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--repair-label',default='market_nontrading_20261013');p.add_argument('--apply',action='store_true');a=p.parse_args()
 api={str(r['announcementId']):r for r in json.loads(a.api_response.read_text())['announcements']};items=json.loads(a.manifest.read_text());prepared=[]
 if not 1<=len(items)<=10:raise ValueError('exact repair limited to 1..10 announcements')
 for x in items:
  r=api[x['announcement_id']];url='https://static.cninfo.com.cn/'+r['adjunctUrl'].lstrip('/')
  if x['source_url']!=url or urlparse(url).hostname!='static.cninfo.com.cn':raise ValueError('official source mismatch')
  pdf=Path(x['pdf']);textpath=Path(x['text']);raw=pdf.read_bytes();text=textpath.read_text()
  if not raw.startswith(b'%PDF') or hashlib.sha256(raw).hexdigest()!=x['pdf_sha256']:raise ValueError('PDF hash mismatch')
  if hashlib.sha256(text.encode()).hexdigest()!=x['text_sha256']:raise ValueError('extracted text hash mismatch')
  if r['secCode'] not in re.sub(r'\s+','',text) or len(text)<200:raise ValueError('security identity or text absent')
  prepared.append({**x,'stock_code':r['secCode'],'title':html.unescape(re.sub('<[^>]+>','',r['announcementTitle'])),
    'announced_at':dt.datetime.fromtimestamp(r['announcementTime']/1000,dt.timezone.utc),'body':text,'raw':raw})
 result={'prepared':len(prepared),'applied':False,'items':[]}
 if a.apply:
  with connect(autocommit=False) as c:
   c.execute("SET LOCAL statement_timeout='25s'");c.execute("SET LOCAL lock_timeout='5s'")
   for r in prepared:
    asset=c.execute("SELECT asset_id FROM market.assets WHERE symbol=%s AND asset_type='stock' AND exchange=%s AND NOT(metadata?'identity_alias_of')",(r['stock_code'],'SSE' if r['stock_code'].startswith('6') else 'SZSE')).fetchone()
    if not asset:raise ValueError('canonical stock missing')
    prior=c.execute("SELECT id,content_hash,length(content_text) n FROM corpus.documents WHERE source_type='announcement' AND source_name='cninfo' AND external_id=%s",(r['announcement_id'],)).fetchone()
    if prior and prior['n'] and prior['content_hash']!=r['text_sha256']:raise ValueError('existing nonempty document differs; review required')
    meta={'stock_code':r['stock_code'],'official_source':True,'pdf_sha256':r['pdf_sha256'],'source_observed_at':r['observed_at'],'targeted_repair':a.repair_label}
    doc=c.execute("""INSERT INTO corpus.documents(source_type,source_name,external_id,title,publisher,published_at,url,content_text,content_hash,metadata,ingest_status)
      VALUES('announcement','cninfo',%s,%s,'巨潮资讯网',%s,%s,%s,%s,%s,'ok')
      ON CONFLICT(source_type,source_name,external_id) DO UPDATE SET content_text=excluded.content_text,content_hash=excluded.content_hash,
       metadata=corpus.documents.metadata||excluded.metadata,updated_at=now() RETURNING id""",
      (r['announcement_id'],r['title'],r['announced_at'],r['source_url'],r['body'],r['text_sha256'],Jsonb(meta))).fetchone()['id']
    c.execute("""INSERT INTO company.filings(asset_id,stock_code,filing_type,title,announced_at,available_at,source_name,source_external_id,source_url,document_id,revision_no,quality_status,metadata)
     VALUES(%s,%s,'announcement',%s,%s,%s,'cninfo',%s,%s,%s,1,'verified',%s)
     ON CONFLICT(source_name,source_external_id,revision_no) DO NOTHING""",
     (asset['asset_id'],r['stock_code'],r['title'],r['announced_at'],r['announced_at'],r['announcement_id'],r['source_url'],doc,Jsonb({**meta,'availability_semantics':'official publication; recorded_at is created_at/updated_at'})))
    c.execute("""INSERT INTO corpus.document_files(document_id,file_role,file_path,file_hash,mime_type,size_bytes,ocr_status)
      VALUES(%s,'pdf',%s,%s,'application/pdf',%s,'ok') ON CONFLICT(document_id,file_role,file_path) DO NOTHING""",
      (doc,str(Path(r['pdf']).resolve()),r['pdf_sha256'],len(r['raw'])))
    result['items'].append({'announcement_id':r['announcement_id'],'document_id':str(doc),'stock_code':r['stock_code'],'prior':dict(prior) if prior else None,'public_available_at':r['announced_at'].isoformat(),'text_sha256':r['text_sha256'],'pdf_sha256':r['pdf_sha256']})
   c.commit();result['applied']=True
 result['recorded_at']=dt.datetime.now(dt.timezone.utc).isoformat();a.output.write_text(json.dumps(result,default=str,ensure_ascii=False,indent=2));print(json.dumps(result,default=str,ensure_ascii=False));return 0
if __name__=='__main__':raise SystemExit(main())
