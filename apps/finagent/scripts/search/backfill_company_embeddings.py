#!/usr/bin/env python3
"""Embed complete available company filing bodies, preserving other model indexes."""

from xtrastock.config import bootstrap_environment
bootstrap_environment()

import argparse,hashlib,json,os,sys,time,re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'scripts/pipeline'),str(ROOT/'scripts/search')]
from common import connect,load_env


def main():
 p=argparse.ArgumentParser();p.add_argument('--stock-code',required=True);p.add_argument('--audit-dir',type=Path,required=True)
 args=p.parse_args();args.audit_dir.mkdir(parents=True,exist_ok=True)
 os.environ.update(load_env(ROOT/'.env.finance-agent-v3'))
 import sync_document_embeddings as w
 with connect() as c:
  docs=c.execute('''SELECT DISTINCT d.id FROM corpus.documents d JOIN company.filings f ON f.document_id=d.id
   WHERE f.stock_code=%s ORDER BY d.id''',(args.stock_code,)).fetchall()
 result=[];claimed=None;start=time.monotonic()
 try:
  for item in docs:
   row=w.fetch_document(item['id'])
   if not row or not (row.get('body') or '').strip():
    result.append({'document_id':str(item['id']),'status':'missing_body'});continue
   chunks=w.chunk_text(row.get('title') or '',row['body'],900,120,100000)
   assert chunks and len(chunks)<100000
   # The heading-aware chunker normalizes paragraph whitespace. Compare the
   # trailing content, not indentation/newline representation.
   end=re.sub(r'\s+','',w.clean_text(row['body']))[-80:]
   assert end in re.sub(r'\s+','',chunks[-1]), 'last source text is not covered'
   sha=hashlib.sha256((w.clean_text(row.get('title'))+'\n'+w.clean_text(row['body'])).encode()).hexdigest()
   with connect() as c:
    q=c.execute('SELECT * FROM ops.document_embedding_queue WHERE document_id=%s FOR UPDATE',(row['id'],)).fetchone()
    if q and q['status']=='running':
     result.append({'document_id':str(row['id']),'status':'busy'});continue
    existing=c.execute(f'''SELECT count(*) n,count(*) FILTER(WHERE e.metadata->>'content_sha256'=%s AND
        coalesce(ch.metadata->>'document_truncated','true')='false') current_n
        FROM {w.EMBEDDING_TABLE} e JOIN corpus.document_chunks ch ON ch.id=e.chunk_id
        WHERE e.document_id=%s AND e.embedding_model=%s''',(sha,row['id'],w.MODEL_NAME)).fetchone()
    fresh=existing['n']==len(chunks)==existing['current_n']
    if not fresh:
     c.execute("UPDATE ops.document_embedding_queue SET status='running',started_at=now() WHERE document_id=%s",(row['id'],));claimed=row['id']
   if not fresh:
    vectors=w.remote_embed(chunks,128)
    assert len(vectors)==len(chunks) and all(len(v)==1024 for v in vectors)
    with connect() as c:
     assert c.execute('SELECT updated_at FROM corpus.documents WHERE id=%s',(row['id'],)).fetchone()['updated_at']==row['updated_at'], 'source changed during inference'
    w.store_document(row,chunks,vectors,False);claimed=None
   entry={'document_id':str(row['id']),'status':'verified_current' if fresh else 'embedded',
    'chunks':len(chunks),'body_chars':len(row['body']),'content_sha256':sha,'last_source_text_covered':True,'truncated':False}
   result.append(entry);print(json.dumps(entry),flush=True)
   (args.audit_dir/'company-embedding-progress.json').write_text(json.dumps(result,indent=2))
 finally:
  if claimed:w.release_unprocessed_claims([{'document_id':claimed}])
  w.refresh_embedding_watermark()
  summary={'selected':len(docs),'results':result,'completed':len(result)==len(docs) and all(r['status'] in ('embedded','verified_current') for r in result),'elapsed_seconds':time.monotonic()-start}
  (args.audit_dir/'company-embedding-result.json').write_text(json.dumps(summary,indent=2))
 return 0 if summary['completed'] else 1
if __name__=='__main__':raise SystemExit(main())
