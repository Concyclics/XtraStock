#!/usr/bin/env python3
"""Retry a saved, finite failure manifest with batched inference and auditable exclusions."""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
from collections import Counter
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'scripts/pipeline'),str(ROOT/'scripts/search')]
from common import connect,load_env


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit-dir',type=Path,required=True)
    parser.add_argument('--documents-per-batch',type=int,default=96)
    args=parser.parse_args()
    audit=args.audit_dir
    manifest=json.loads((audit/'failed-embedding-queue-before.json').read_text())
    os.environ.update(load_env(ROOT/'.env.finance-agent-v3'))
    import sync_document_embeddings as worker
    if worker.EMBEDDING_PROVIDER!='openai_compatible' or worker.MODEL_DIMENSIONS!=1024:
        raise RuntimeError('unexpected embedding backend; refuse to replace vectors')
    checkpoint=audit/'embedding-retry-items.jsonl'
    finished={}
    if checkpoint.exists():
        for line in checkpoint.read_text().splitlines():
            row=json.loads(line)
            if row['status'] in ('ok','excluded_duplicate','already_completed'):
                finished[row['document_id']]=row
    def record(item):
        item['recorded_at']=dt.datetime.now(dt.timezone.utc).isoformat()
        with checkpoint.open('a') as f:
            f.write(json.dumps(item,ensure_ascii=False,default=str)+'\n')
            f.flush()
        finished[item['document_id']]=item
    started=time.monotonic()
    pending=[r['document_id'] for r in manifest if r['document_id'] not in finished]
    errors=[];claimed=[]
    try:
        for start in range(0,len(pending),args.documents_per_batch):
            ids=pending[start:start+args.documents_per_batch]
            with connect() as conn:
                rows=conn.execute('''
                    SELECT d.id,d.source_type,d.source_name,d.published_at,d.title,d.updated_at,
                           coalesce(d.content_text,d.content_md,d.raw_text,'') AS body,
                           q.status AS queue_status,q.embedding_model AS queue_model,
                           dq.quality_status,dq.duplicate_of,
                           EXISTS(SELECT 1 FROM corpus.document_quarantine x WHERE x.document_id=d.id AND x.released_at IS NULL) AS quarantined
                    FROM ops.document_embedding_queue q JOIN corpus.documents d ON d.id=q.document_id
                    LEFT JOIN corpus.document_quality dq ON dq.document_id=d.id
                    WHERE q.document_id=ANY(%s::uuid[]) FOR UPDATE OF q SKIP LOCKED
                ''',(ids,)).fetchall()
                claimed=[]
                for row in rows:
                    key=str(row['id'])
                    if row['queue_status']=='running':
                        continue
                    if row['quarantined'] or row['quality_status'] in ('template_noise','parse_failed','quarantined'):
                        errors.append({'document_id':key,'reason':'active quality/quarantine restriction'})
                        continue
                    if row['quality_status']=='duplicate':
                        conn.execute("UPDATE ops.document_embedding_queue SET status='skipped',processed_at=now(),started_at=NULL,last_error=%s WHERE document_id=%s",
                                     ('Excluded: confirmed duplicate; original failure is preserved in retry audit',row['id']))
                        record({'document_id':key,'status':'excluded_duplicate','duplicate_of':str(row['duplicate_of'])})
                        continue
                    if row['queue_status']=='ok' and row['queue_model']==worker.MODEL_NAME:
                        record({'document_id':key,'status':'already_completed'})
                        continue
                    conn.execute("UPDATE ops.document_embedding_queue SET status='running',started_at=now() WHERE document_id=%s",(row['id'],))
                    claimed.append(row)
            if not claimed:
                continue
            chunks=[];batches=[]
            for row in claimed:
                pieces=worker.chunk_text(row.get('title') or '',row.get('body') or '',900,120,1000)
                if not pieces:
                    raise RuntimeError('empty document encountered; retain failure for inspection')
                if len(pieces)>=1000:
                    raise RuntimeError('document exceeds full-content retry chunk bound')
                batches.append((row,len(chunks),len(pieces)))
                chunks.extend(pieces)
            vectors=worker.remote_embed(chunks,128)
            if len(vectors)!=len(chunks) or any(len(v)!=1024 for v in vectors):
                raise RuntimeError('embedding response count/dimensions mismatch')
            for row,offset,count in batches:
                # Do not acknowledge an old body if ingestion changed it while
                # inference was running. The source remains queued for a retry.
                with connect() as conn:
                    current=conn.execute('SELECT updated_at FROM corpus.documents WHERE id=%s',(row['id'],)).fetchone()
                if not current or current['updated_at']!=row['updated_at']:
                    worker.release_unprocessed_claims([{'document_id':row['id']}])
                    errors.append({'document_id':str(row['id']),'reason':'body changed during inference'})
                    continue
                worker.store_document(row,chunks[offset:offset+count],vectors[offset:offset+count],False)
                record({'document_id':str(row['id']),'status':'ok','chunks':count,'model':worker.MODEL_NAME,'dimensions':1024})
            claimed=[]
            counts=Counter(r['status'] for r in finished.values())
            progress={'target_documents':len(manifest),'finished':len(finished),'counts':dict(counts),
                      'elapsed_seconds':round(time.monotonic()-started,2),'errors':errors,
                      'remaining':len(manifest)-len(finished),'source':'finite saved failure manifest'}
            (audit/'embedding-retry-progress.json').write_text(json.dumps(progress,ensure_ascii=False,indent=2))
            print(json.dumps(progress,ensure_ascii=False),flush=True)
    except Exception as exc:
        errors.append({'error_type':type(exc).__name__,'error':str(exc)[:700]})
        raise
    finally:
        if claimed:
            worker.release_unprocessed_claims([{'document_id':r['id']} for r in claimed])
        worker.refresh_embedding_watermark()
        counts=Counter(r['status'] for r in finished.values())
        result={'target_documents':len(manifest),'finished':len(finished),'counts':dict(counts),
                'remaining':len(manifest)-len(finished),'errors':errors,
                'elapsed_seconds':round(time.monotonic()-started,2),'model':worker.MODEL_NAME,
                'completed':len(finished)==len(manifest) and not errors,
                'old_models_preserved':True}
        (audit/'embedding-retry-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(result,ensure_ascii=False),flush=True)
    return 0 if result['completed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
