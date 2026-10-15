"""Resumable, bounded official filing body ingestion, independent of OCR queue size."""
from __future__ import annotations
import argparse,hashlib,json,os,re,shutil,subprocess,sys,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from psycopg.types.json import Jsonb

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts/search'))
from common import connect,load_env
from score_document_quality_v3 import score_row


def seed_and_claim(limit, stock_code=None, seed=False):
    with connect() as c:
        if seed:
            c.execute("""INSERT INTO ops.cninfo_body_queue(document_id)
              SELECT id FROM corpus.documents WHERE source_type='announcement' AND source_name='cninfo'
              ON CONFLICT DO NOTHING""")
            # Resume OCR-required files through the existing on-demand worker.
            needs=c.execute("""SELECT q.document_id,f.file_path FROM ops.cninfo_body_queue q
              JOIN corpus.document_files f ON f.document_id=q.document_id AND f.file_role='pdf'
              WHERE q.status='needs_ocr'""").fetchall()
            for row in needs:
                enqueue_ocr(c,row['document_id'],Path(row['file_path']))
        c.execute("""UPDATE ops.cninfo_body_queue SET status='pending',started_at=NULL,
          last_error='recovered interrupted body acquisition',updated_at=now()
          WHERE status='running' AND started_at<now()-interval '1 hour'""")
        return c.execute("""WITH selected AS (
          SELECT q.document_id FROM ops.cninfo_body_queue q JOIN corpus.documents d ON d.id=q.document_id
          WHERE q.status IN ('pending','failed') AND q.attempts<4 AND q.next_attempt_at<=now()
            AND (%(stock)s::text IS NULL OR d.metadata->>'stock_code'=%(stock)s)
            AND NOT EXISTS(SELECT 1 FROM corpus.document_quarantine x WHERE x.document_id=d.id AND x.released_at IS NULL)
          ORDER BY EXISTS(SELECT 1 FROM agent.watchlist_items w WHERE w.stock_code=d.metadata->>'stock_code' AND w.removed_at IS NULL) DESC,
             d.published_at DESC NULLS LAST,d.id
          FOR UPDATE OF q SKIP LOCKED LIMIT %(limit)s
        ), claimed AS (
          UPDATE ops.cninfo_body_queue q SET status='running',started_at=now(),attempts=attempts+1,updated_at=now()
          FROM selected s WHERE q.document_id=s.document_id RETURNING q.document_id
        ) SELECT d.id,d.title,d.url,d.external_id,d.published_at,d.metadata,d.source_type,d.source_name,
            coalesce(nullif(d.content_text,''),nullif(d.content_md,''),d.raw_text,'') AS body,
            f.file_path
          FROM claimed q JOIN corpus.documents d ON d.id=q.document_id
          LEFT JOIN LATERAL (SELECT file_path FROM corpus.document_files WHERE document_id=d.id AND file_role='pdf' ORDER BY file_path LIMIT 1) f ON true
        """,{'stock':stock_code,'limit':limit}).fetchall()


def extract_native_pdf(path):
    with path.open('rb') as stream:
        if not stream.read(8).startswith(b'%PDF'):
            raise ValueError('local_attachment_not_pdf')
    result=subprocess.run(['pdftotext','-layout','-enc','UTF-8',str(path),'-'],capture_output=True,text=True,timeout=120)
    if result.returncode:
        raise ValueError('pdftotext_failed: '+result.stderr[-500:])
    return result.stdout.replace('\x00','')


def acquire(row):
    if row['body'].strip():
        return {'text':row['body'],'method':'existing_body','path':row.get('file_path')}
    path=Path(row['file_path']) if row.get('file_path') else None
    url=urlparse(row['url'] or '')
    if url.scheme!='https' or url.hostname!='static.cninfo.com.cn' or not url.path.lower().endswith('.pdf'):
        raise ValueError('unsupported_official_attachment_url')
    if path is None or not path.is_file():
        external=str(row['external_id'])
        if not external.isdigit(): raise ValueError('invalid_official_attachment_id')
        at=row['published_at']
        path=ROOT/'filings/cninfo'/str(at.year)/f'{at.month:02d}'/(external+'.pdf')
    if path.stem != str(row['external_id']):
        raise ValueError('attachment_id_does_not_match_official_record')
    text = None
    if path.is_file():
        try:
            text = extract_native_pdf(path)
        except (ValueError, subprocess.TimeoutExpired):
            # Preserve the damaged download before fetching the same official
            # attachment again. Never replace it with another invalid response.
            digest=hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            archived=path.with_name(path.name+'.invalid-'+digest)
            if not archived.exists():shutil.copy2(path,archived)
    if text is None:
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.pdf.partial')
        session=requests.Session();session.headers.update({'User-Agent':'Mozilla/5.0','Referer':'https://www.cninfo.com.cn/'})
        session.mount('https://',HTTPAdapter(max_retries=Retry(total=2,backoff_factor=1,status_forcelist=(429,500,502,503,504))))
        try:
            with session.get(row['url'],stream=True,timeout=(10,45),allow_redirects=False) as response:
                response.raise_for_status()
                if response.is_redirect: raise ValueError('unexpected_attachment_redirect')
                length=0
                with temporary.open('wb') as stream:
                    for block in response.iter_content(65536):
                        length+=len(block)
                        if length>256*1024*1024:raise ValueError('attachment_size_limit_256MiB')
                        stream.write(block)
            with temporary.open('rb') as stream:
                if not stream.read(8).startswith(b'%PDF'):raise ValueError('attachment_not_pdf')
            text = extract_native_pdf(temporary)
            os.replace(temporary,path)
        finally:
            session.close()
    if len(re.sub(r'\s+','',text))<100:return {'needs_ocr':True,'path':str(path),'error':'native_text_insufficient'}
    code=str(row['metadata'].get('stock_code') or '')
    name=re.sub(r'\s+','',str(row['metadata'].get('stock_name') or '')).replace('*','')
    normalized=re.sub(r'\s+','',text)
    # Official attachments need an issuer marker when metadata supplies one.
    # Short stock names may omit ST; a code anywhere in the source is preferred.
    issuer_marker_found = not code or code in normalized or bool(name and name in normalized)
    if not issuer_marker_found:
        short_name=re.sub(r'^(?:\*?ST|[CN])','',name,flags=re.I)
        issuer_marker_found = len(short_name)>=2 and short_name in normalized
    # H-share attachments use traditional/English names and HK codes. Their
    # official attachment ID binds ownership; an absent A-share text marker is
    # recorded separately, rather than discarding a correctly linked document.
    return {'text':text,'path':str(path),'method':'pdftotext:layout:utf8','issuer_marker_found':issuer_marker_found}


def enqueue_ocr(c,document_id,path):
    directory=ROOT/'filings/cninfo_ocr';directory.mkdir(parents=True,exist_ok=True)
    stem=directory/path.stem
    c.execute("""INSERT INTO ops.ocr_queue(source_name,report_type,pdf_path,md_path,
      quality_json_path,quality_md_path,status,priority,metadata)
      VALUES('cninfo','announcement',%s,%s,%s,%s,'pending',120,%s)
      ON CONFLICT(pdf_path) DO NOTHING""",
      (str(path),str(stem.with_suffix('.md')),str(stem.with_name(stem.name+'_quality.json')),
       str(stem.with_name(stem.name+'_quality.md')),Jsonb({'document_id':str(document_id),'input_signature':f'{path.stat().st_size}:{path.stat().st_mtime_ns}'})))


def persist(row,result):
    with connect() as c:
        if result.get('needs_ocr'):
            c.execute("""INSERT INTO corpus.document_files(document_id,file_role,file_path,mime_type,size_bytes,ocr_status)
                VALUES(%s,'pdf',%s,'application/pdf',%s,'pending') ON CONFLICT(document_id,file_role,file_path) DO NOTHING""",
                (row['id'],result['path'],Path(result['path']).stat().st_size))
            c.execute("UPDATE ops.cninfo_body_queue SET status='needs_ocr',last_error=%s,finished_at=now(),updated_at=now() WHERE document_id=%s",(result['error'],row['id']))
            enqueue_ocr(c,row['id'],Path(result['path']))
            return 'needs_ocr'
        text=result['text'];digest=hashlib.sha256(text.encode()).hexdigest()
        # Lock and recheck: a concurrent financial parser may already have filled it.
        current=c.execute("SELECT content_text,content_md,raw_text FROM corpus.documents WHERE id=%s FOR UPDATE",(row['id'],)).fetchone()
        stored=next((current[k] for k in ('content_text','content_md','raw_text') if current[k]),'')
        if stored and stored!=text: text=stored;digest=hashlib.sha256(text.encode()).hexdigest()
        if not stored:
            c.execute("""UPDATE corpus.documents SET content_text=%s,
              metadata=metadata||%s,updated_at=now() WHERE id=%s""",
              (text,Jsonb({'body_parser':result['method'],'body_sha256':digest,
                'body_origin':'cninfo_official_attachment_registry','issuer_text_marker_found':result.get('issuer_marker_found')}),row['id']))
        scored=score_row({**row,'body':text})
        c.execute("""INSERT INTO corpus.document_quality(document_id,quality_status,quality_score,title_quality_score,
            body_quality_score,template_similarity,body_chars,parser_version,content_version,checks,checked_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,%s,now()) ON CONFLICT(document_id) DO UPDATE SET
            quality_status=excluded.quality_status,quality_score=excluded.quality_score,title_quality_score=excluded.title_quality_score,
            body_quality_score=excluded.body_quality_score,body_chars=excluded.body_chars,parser_version=excluded.parser_version,
            checks=corpus.document_quality.checks||excluded.checks,checked_at=now()""",
            tuple(scored[k] for k in ('document_id','quality_status','quality_score','title_quality_score','body_quality_score','template_similarity','body_chars','parser_version'))+(Jsonb(scored['checks']),))
        if result.get('path'):
            path=Path(result['path'])
            c.execute("""INSERT INTO corpus.document_files(document_id,file_role,file_path,mime_type,size_bytes,ocr_status,ocr_quality)
                VALUES(%s,'pdf',%s,'application/pdf',%s,'ok',%s) ON CONFLICT(document_id,file_role,file_path) DO UPDATE SET
                ocr_status='ok',ocr_quality=excluded.ocr_quality""",
                (row['id'],str(path),path.stat().st_size,Jsonb({'method':result['method'],'body_chars':len(text),'text_sha256':digest})))
        c.execute("""UPDATE ops.cninfo_body_queue SET status='ok',finished_at=now(),body_chars=%s,text_sha256=%s,
          last_error=NULL,updated_at=now() WHERE document_id=%s""",(len(text),digest,row['id']))
        # Requeue metadata-only old indices too, even when the body already existed.
        c.execute("SELECT ops.enqueue_document_index(%s,'cninfo_body_ready',30)",(row['id'],))
    return 'ok'


def process(row):
    try:return {'document_id':str(row['id']),'status':persist(row,acquire(row))}
    except Exception as exc:
        error=f'{type(exc).__name__}: {exc}'[:700]
        with connect() as c:
            c.execute("""UPDATE ops.cninfo_body_queue SET status='failed',last_error=%s,finished_at=now(),
                next_attempt_at=now()+interval '30 minutes',updated_at=now() WHERE document_id=%s""",(error,row['id']))
        return {'document_id':str(row['id']),'status':'failed','error':error}


def main():
    p=argparse.ArgumentParser();p.add_argument('--limit',type=int,default=1000);p.add_argument('--max-seconds',type=int,default=900)
    p.add_argument('--workers',type=int,default=3);p.add_argument('--stock-code');p.add_argument('--audit-dir',type=Path)
    args=p.parse_args();started=time.monotonic();counts={};processed=0
    if args.audit_dir:args.audit_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    while processed<args.limit and time.monotonic()-started<args.max_seconds:
        batch=seed_and_claim(min(50,args.limit-processed),args.stock_code,seed=processed==0)
        if not batch:break
        with ThreadPoolExecutor(max_workers=max(1,min(args.workers,4))) as pool:
            for outcome in pool.map(process,batch):
                counts[outcome['status']]=counts.get(outcome['status'],0)+1;processed+=1
                if args.audit_dir:
                    with (args.audit_dir/'body-outcomes.jsonl').open('a') as stream:stream.write(json.dumps(outcome,ensure_ascii=False)+'\n')
                if outcome['status']!='ok':print(json.dumps(outcome,ensure_ascii=False),flush=True)
        print(json.dumps({'processed':processed,'counts':counts,'seconds':round(time.monotonic()-started,1)},ensure_ascii=False),flush=True)
    print(json.dumps({'final':True,'processed':processed,'counts':counts,'seconds':round(time.monotonic()-started,1)},ensure_ascii=False))
    return 1 if counts.get('failed') else 0

if __name__=='__main__':raise SystemExit(main())
