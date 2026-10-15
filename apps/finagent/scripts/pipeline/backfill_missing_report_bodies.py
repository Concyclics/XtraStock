"""Recover missing report text from identity-checked original PDF attachments.

Does not mark OCR complete or treat extracted text as structured financial facts.
Per-document commits, durable attempts, raw PDFs and exact provenance are retained.
"""
from __future__ import annotations
import argparse,concurrent.futures,datetime as dt,hashlib,json,re,subprocess,time
from pathlib import Path
import requests
from psycopg.types.json import Jsonb
from common import ROOT,connect


def identity_supported(text,row):
 normal=lambda x:re.sub(r'\W+','',str(x))
 title=normal(row['report_title']);body=normal(text)
 if len(title)>=8 and title in body:return 'exact_report_title'
 # Provider titles often prefix the actual cover title with an industry or
 # report series. Accept the full distinctive subtitle only on the cover,
 # with a matching publication date and the original infocode attachment.
 parts=re.split(r'[:：]',str(row['report_title']),maxsplit=1)
 if len(parts)==2 and re.fullmatch(r'AP\d{12,}',str(row.get('eastmoney_infocode') or '')) and row.get('report_date'):
  subtitle=normal(parts[1]);expected=dt.date.fromisoformat(str(row['report_date'])[:10])
  dates=[]
  for y,m,d in re.findall(r'(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})',text[:2500]):
   try:dates.append(dt.date(int(y),int(m),int(d)))
   except ValueError:pass
  if len(subtitle)>=12 and subtitle in normal(text[:1800]) and any(abs((date-expected).days)<=3 for date in dates):
   return 'original_infocode_cover_subtitle_and_date'
 code=str(row.get('stock_code') or '')
 if row['report_type']=='stock' and re.fullmatch(r'\d{6}',code) and re.search(r'(?<!\d)'+code+r'(?!\d)',text):
  return 'original_infocode_url_and_security_code'
 raise ValueError('attachment identity requires manual review')


def acquire(row):
 ident=row['eastmoney_infocode']
 if not re.fullmatch(r'AP\d{12,}',ident):raise ValueError('invalid report identity')
 path=ROOT/'filings/recovered_reports'/(ident+'.pdf');path.parent.mkdir(parents=True,exist_ok=True)
 url='https://pdf.dfcfw.com/pdf/H3_'+ident+'_1.pdf'
 if not path.exists():
  partial=path.with_suffix('.pdf.partial')
  with requests.get(url,stream=True,timeout=(8,30),allow_redirects=False,headers={'User-Agent':'Mozilla/5.0','Referer':'https://data.eastmoney.com/'}) as res:
   res.raise_for_status()
   if res.is_redirect:raise ValueError('unexpected redirect')
   size=0
   with partial.open('wb') as stream:
    for block in res.iter_content(65536):
     size+=len(block)
     if size>64*1024*1024:raise ValueError('PDF exceeds bounded 64MiB download')
     stream.write(block)
  with partial.open('rb') as stream:
   if not stream.read(8).startswith(b'%PDF'):raise ValueError('original attachment unavailable or non-PDF')
  partial.replace(path)
 # Separate processes isolate PDF parsers and bound CPU time per attachment.
 info=subprocess.run(['pdfinfo',str(path)],capture_output=True,text=True,timeout=30)
 if info.returncode:raise ValueError('PDF structural parse failed')
 match=re.search(r'^Pages:\s*(\d+)',info.stdout,re.M)
 if not match or not 1<=int(match[1])<=600:raise ValueError('page count outside bounded scope')
 result=subprocess.run(['pdftotext','-layout','-enc','UTF-8',str(path),'-'],capture_output=True,text=True,timeout=60)
 if result.returncode:raise ValueError('PDF text extraction failed')
 text=result.stdout.replace('\x00','');pages=text.split('\f');count=int(match[1])
 if len(pages)<count:raise ValueError('PDF text page sequence incomplete')
 identity=identity_supported('\n'.join(pages[:2]),row)
 useful=sum(len(re.sub(r'\s+','',page))>=30 for page in pages[:count])
 if len(re.sub(r'\s+','',text))<100:raise ValueError('original PDF needs OCR')
 return {'text':text,'path':path,'url':url,'pages':count,'pages_with_text':useful,'identity':identity,
  'sha':hashlib.sha256(path.read_bytes()).hexdigest(),'body_sha':hashlib.sha256(text.encode()).hexdigest(),
  'parser_warnings':bool(result.stderr.strip())}


def persist(row,out):
 from scripts.search.score_document_quality_v3 import score_row
 with connect() as c:
  c.execute("SET LOCAL lock_timeout='3s'");c.execute("SET LOCAL statement_timeout='25s'")
  current=c.execute('SELECT content_text,content_md,raw_text FROM corpus.documents WHERE id=%s FOR UPDATE',(row['id'],)).fetchone()
  if any(current[k] for k in ('content_text','content_md','raw_text')):
   c.execute("UPDATE ops.document_body_recovery SET status='superseded_by_other_writer',finished_at=now(),updated_at=now() WHERE document_id=%s",(row['id'],));return 'superseded'
  provenance={k:out[k] for k in ('url','pages','pages_with_text','identity','sha','body_sha','parser_warnings')}
  provenance.update(method='pdftotext_layout',full_visual_ocr_verified=False,recovered_at=dt.datetime.now(dt.timezone.utc).isoformat())
  c.execute("UPDATE corpus.documents SET content_text=%s,content_hash=%s,metadata=metadata||%s,updated_at=now() WHERE id=%s",(out['text'],out['body_sha'],Jsonb({'body_recovery':provenance}),row['id']))
  c.execute("""INSERT INTO corpus.document_files(document_id,file_role,file_path,file_hash,mime_type,size_bytes,page_count,ocr_status,ocr_quality)
   VALUES(%s,'pdf',%s,%s,'application/pdf',%s,%s,'pending',%s) ON CONFLICT(document_id,file_role,file_path) DO NOTHING""",
   (row['id'],str(out['path']),out['sha'],out['path'].stat().st_size,out['pages'],Jsonb(provenance)))
  q=score_row({**row,'body':out['text']})
  c.execute("""INSERT INTO corpus.document_quality(document_id,quality_status,quality_score,title_quality_score,body_quality_score,
   template_similarity,body_chars,parser_version,content_version,checks,checked_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,%s,now())
   ON CONFLICT(document_id) DO UPDATE SET quality_status=excluded.quality_status,quality_score=excluded.quality_score,
   body_chars=excluded.body_chars,checks=corpus.document_quality.checks||excluded.checks,checked_at=now()""",
   tuple(q[k] for k in ('document_id','quality_status','quality_score','title_quality_score','body_quality_score','template_similarity','body_chars','parser_version'))+(Jsonb(q['checks']),))
  c.execute("SELECT ops.enqueue_document_index(%s,'recovered_report_text',20)",(row['id'],))
  c.execute("""UPDATE ops.document_body_recovery SET status='text_recovered',source_url=%s,source_sha256=%s,body_sha256=%s,
   page_count=%s,pages_with_text=%s,last_error=NULL,finished_at=now(),updated_at=now() WHERE document_id=%s""",
   (out['url'],out['sha'],out['body_sha'],out['pages'],out['pages_with_text'],row['id']))
 return 'text_recovered'


def process(row):
 with connect() as c:
  claimed=c.execute("""INSERT INTO ops.document_body_recovery(document_id,status,attempts,started_at)
    VALUES(%s,'running',1,now()) ON CONFLICT(document_id) DO UPDATE SET status='running',attempts=ops.document_body_recovery.attempts+1,
    started_at=now(),updated_at=now() WHERE ops.document_body_recovery.status<>'running' RETURNING document_id""",(row['id'],)).fetchone()
 if not claimed:return {'document_id':str(row['id']),'status':'already_running'}
 try:status=persist(row,acquire(row));error=None
 except Exception as exc:
  status='source_unavailable_or_needs_review';error=type(exc).__name__+': '+str(exc)[:250]
  with connect() as c:c.execute("UPDATE ops.document_body_recovery SET status=%s,last_error=%s,finished_at=now(),updated_at=now() WHERE document_id=%s",(status,error,row['id']))
 return {'document_id':str(row['id']),'status':status,'error':error}


def main():
 import sys
 if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
 p=argparse.ArgumentParser();p.add_argument('--limit',type=int,default=100);p.add_argument('--workers',type=int,default=3);p.add_argument('--audit-dir',type=Path,required=True)
 p.add_argument('--retry-identity',action='store_true',help='Retry only prior identity-review failures against the stricter cover/date rule')
 args=p.parse_args()
 args.audit_dir.mkdir(parents=True,exist_ok=True)
 with connect() as c:
  c.execute("SET LOCAL statement_timeout='20s'")
  rows=c.execute("""SELECT d.id,d.title,d.source_type,d.source_name,r.eastmoney_infocode,r.report_type,r.stock_code,r.stock_name,r.report_title,r.report_date
   FROM corpus.documents d JOIN research.reports r ON r.document_id=d.id
   WHERE coalesce(d.content_text,'')='' AND coalesce(d.content_md,'')='' AND coalesce(d.raw_text,'')=''
     AND CASE WHEN %s THEN EXISTS(SELECT 1 FROM ops.document_body_recovery b WHERE b.document_id=d.id AND b.status='source_unavailable_or_needs_review' AND b.last_error LIKE '%%identity requires manual review%%')
       ELSE NOT EXISTS(SELECT 1 FROM ops.document_body_recovery b WHERE b.document_id=d.id AND b.updated_at>now()-interval '7 days') END
   ORDER BY r.report_date DESC,d.id LIMIT %s""",(args.retry_identity,max(1,min(args.limit,10000)),)).fetchall()
 counts={};start=time.monotonic()
 with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,min(args.workers,4))) as pool:
  for outcome in pool.map(process,rows):
   counts[outcome['status']]=counts.get(outcome['status'],0)+1
   with (args.audit_dir/'outcomes.jsonl').open('a') as f:f.write(json.dumps(outcome,ensure_ascii=False)+'\n')
   result={'selected':len(rows),'processed':sum(counts.values()),'counts':counts,'seconds':round(time.monotonic()-start,2)}
   (args.audit_dir/'progress.json').write_text(json.dumps(result,indent=2))
   if result['processed']%25==0:print(json.dumps(result),flush=True)
 print(json.dumps(result if rows else {'selected':0,'counts':counts}),flush=True)
 return 2 if counts.get('source_unavailable_or_needs_review') else 0
if __name__=='__main__':raise SystemExit(main())
