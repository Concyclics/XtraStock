#!/usr/bin/env python3
"""Download and index missing official filing bodies for one issuer."""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

import requests
from psycopg.types.json import Jsonb
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/pipeline'), str(ROOT / 'scripts/search'), str(ROOT / 'scripts/analytics')]
from common import connect, load_env
from extract_financial_facts_cninfo import store_pdf_text
from score_document_quality_v3 import score_row
import sync_document_search as search


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock-code', required=True)
    parser.add_argument('--audit-dir', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]{6}', args.stock_code):
        parser.error('stock-code must be six digits')
    audit = args.audit_dir
    audit.mkdir(mode=0o700, parents=True, exist_ok=True)
    with connect() as conn:
        rows = conn.execute('''
            SELECT DISTINCT ON (d.id) d.id,d.title,d.url,d.source_type,d.source_name,
                   d.published_at,d.updated_at,d.metadata,
                   coalesce(d.content_text,d.content_md,d.raw_text,'') AS body,
                   f.source_external_id,f.announced_at,f.metadata->>'stock_name' AS stock_name,a.name AS asset_name,
                   q.quality_status,q.duplicate_of,
                   EXISTS (SELECT 1 FROM corpus.document_quarantine x
                           WHERE x.document_id=d.id AND x.released_at IS NULL) AS quarantined
            FROM company.filings f JOIN corpus.documents d ON d.id=f.document_id
            JOIN market.assets a ON a.asset_id=f.asset_id
            LEFT JOIN corpus.document_quality q ON q.document_id=d.id
            WHERE f.source_name='cninfo' AND f.stock_code=%s
            ORDER BY d.id,f.revision_no DESC
        ''', (args.stock_code,)).fetchall()
    before = audit / 'filing-bodies-before.json'
    if not before.exists():
        before.write_text(json.dumps(rows, ensure_ascii=False, default=str, indent=2))
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.cninfo.com.cn/'})
    session.mount('https://', HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1,
        status_forcelist=(429,500,502,503,504), allowed_methods={'GET'})))
    results = []
    for row in rows:
        item = {'document_id': str(row['id']), 'title': row['title'], 'url': row['url']}
        try:
            if row['quarantined']:
                raise RuntimeError('document has active quarantine; manual review needed')
            if row['body'].strip():
                item.update(status='already_present', text_chars=len(row['body']))
            else:
                url = urlparse(row['url'] or '')
                if url.scheme != 'https' or url.hostname != 'static.cninfo.com.cn' or not url.path.lower().endswith('.pdf'):
                    raise ValueError('not an official CNINFO PDF URL')
                external = str(row['source_external_id'])
                if not external.isdigit():
                    raise ValueError('invalid announcement ID')
                at = row['announced_at']
                path = ROOT / 'filings/cninfo' / str(at.year) / f'{at.month:02d}' / (external + '.pdf')
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    temp = path.with_suffix('.pdf.partial')
                    total = 0
                    with session.get(row['url'], timeout=(10,60), stream=True) as response:
                        response.raise_for_status()
                        with temp.open('wb') as stream:
                            for block in response.iter_content(65536):
                                total += len(block)
                                if total > 64 * 1024 * 1024:
                                    raise ValueError('PDF exceeds the 64 MiB per-file limit')
                                stream.write(block)
                    if not temp.read_bytes().startswith(b'%PDF'):
                        raise ValueError('downloaded content is not PDF')
                    os.replace(temp, path)
                data = path.read_bytes()
                if not data.startswith(b'%PDF'):
                    raise ValueError('existing local file is not PDF')
                text = subprocess.run(['pdftotext','-layout','-enc','UTF-8',str(path),'-'],
                                      check=True, capture_output=True, text=True, timeout=120).stdout
                compact = re.sub(r'\s+', '', text)
                if len(compact) < 100:
                    raise ValueError('insufficient extracted text; requires OCR')
                issuer_names = [re.sub(r'\s+', '', str(name or '')) for name in (row['stock_name'],row['asset_name'])]
                if args.stock_code not in compact and not any(name and name in compact for name in issuer_names):
                    raise ValueError('PDF issuer identity does not match the requested stock')
                scored = score_row({**row, 'body': text})
                if scored['quality_status'] != 'accepted':
                    raise ValueError('extracted body failed document quality check')
                text_hash = hashlib.sha256(text.encode()).hexdigest()
                with connect() as conn:
                    old = conn.execute('SELECT id FROM corpus.documents WHERE id=%s FOR UPDATE', (row['id'],)).fetchone()
                    if not old:
                        raise RuntimeError('document no longer exists')
                    store_pdf_text(conn, row['id'], text)
                    stored = conn.execute('SELECT content_text FROM corpus.documents WHERE id=%s', (row['id'],)).fetchone()['content_text']
                    if stored != text:
                        raise RuntimeError('existing/concurrently supplied body differs; not overwritten')
                    conn.execute('''INSERT INTO corpus.document_files (
                        document_id,file_role,file_path,file_hash,mime_type,size_bytes,ocr_status,ocr_quality
                        ) VALUES (%s,'pdf',%s,%s,'application/pdf',%s,'ok',%s)
                        ON CONFLICT (document_id,file_role,file_path) DO UPDATE SET
                          file_hash=EXCLUDED.file_hash,size_bytes=EXCLUDED.size_bytes,
                          ocr_status='ok',ocr_quality=EXCLUDED.ocr_quality''',
                        (row['id'],str(path),hashlib.sha256(data).hexdigest(),len(data),
                         Jsonb({'method':'pdftotext','text_chars':len(text),'text_sha256':text_hash})))
                    conn.execute('''INSERT INTO corpus.document_quality (
                        document_id,quality_status,quality_score,title_quality_score,body_quality_score,
                        template_similarity,body_chars,parser_version,content_version,checks,checked_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,%s,now())
                        ON CONFLICT(document_id) DO UPDATE SET quality_status=EXCLUDED.quality_status,
                          duplicate_of=NULL,quality_score=EXCLUDED.quality_score,
                          title_quality_score=EXCLUDED.title_quality_score,body_quality_score=EXCLUDED.body_quality_score,
                          template_similarity=EXCLUDED.template_similarity,body_chars=EXCLUDED.body_chars,
                          parser_version=EXCLUDED.parser_version,checks=EXCLUDED.checks,checked_at=now()''',
                        tuple(scored[k] for k in ('document_id','quality_status','quality_score','title_quality_score',
                              'body_quality_score','template_similarity','body_chars','parser_version'))+(Jsonb(scored['checks']),))
                item.update(status='completed', path=str(path), pdf_sha256=hashlib.sha256(data).hexdigest(),
                            text_sha256=text_hash, text_chars=len(text), database_text_equal=True)
                time.sleep(0.15)
        except Exception as exc:
            item.update(status='failed', error_type=type(exc).__name__, error=str(exc)[:700])
        results.append(item)
        (audit/'filing-bodies-progress.json').write_text(json.dumps(results,ensure_ascii=False,default=str,indent=2))
        print(json.dumps(item,ensure_ascii=False),flush=True)
    ids=[r['id'] for r in rows]
    fetched=search.fetch_documents(None,None,len(ids)+1,0,6000,False,document_ids=ids) if ids else []
    built=[search.build_search_row(row,6000) for row in fetched]
    search.upsert_fts(built)
    env=load_env()
    indexed=search.upsert_meili(built,os.getenv('MEILI_URL','http://127.0.0.1:17700'),
        os.getenv('MEILI_MASTER_KEY',env.get('MEILI_MASTER_KEY','')),os.getenv('MEILI_INDEX',search.DEFAULT_INDEX),100,1800)
    summary={'stock_code':args.stock_code,'selected':len(rows),'filled':sum(r['status']=='completed' for r in results),
             'already_present':sum(r['status']=='already_present' for r in results),
             'failed':[r for r in results if r['status']=='failed'],'fts_indexed':len(built),'meili_indexed':indexed}
    (audit/'filing-bodies-result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return 1 if summary['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
