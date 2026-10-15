#!/usr/bin/env python3
"""Bounded cross-document embedding batches with version-checked bulk commits.

Uses the existing authenticated local gateway and preserves other model indexes.
No document is marked complete until every chunk has been stored atomically.
"""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/pipeline'))
from common import connect, load_env


def prepare(row, worker):
    title, body = worker.clean_text(row['title']), worker.clean_text(row['body'])
    # Long titles must not consume the complete window and prevent advancement.
    chunks = worker.chunk_text(title[:180], body or title, 900, 120, 100000, require_complete=True)
    if not chunks:
        raise ValueError('empty document; no vector can be validated')
    # The chunker verifies it reached the end of the source. A final window can
    # contain fewer than 80 non-space characters; matching an 80-character tail
    # only in that window wrongly rejects completely covered documents.
    return {**row, 'chunks': chunks, 'digest': hashlib.sha256((title+'\n'+body).encode()).hexdigest()}


def groups(documents, chunk_budget):
    group, size = [], 0
    for doc in documents:
        if group and size + len(doc['chunks']) > chunk_budget:
            yield group
            group, size = [], 0
        group.append(doc)
        size += len(doc['chunks'])
    if group:
        yield group


def assign_vectors(documents, vectors, dimensions):
    expected = sum(len(doc['chunks']) for doc in documents)
    if len(vectors) != expected:
        raise ValueError('embedding API returned an unexpected item count')
    if any(len(v) != dimensions or not all(math.isfinite(float(x)) for x in v) for v in vectors):
        raise ValueError('invalid embedding dimension or non-finite values')
    offset = 0
    for doc in documents:
        end = offset + len(doc['chunks'])
        doc['vectors'] = vectors[offset:end]
        offset = end


def claim(conn, limit, model, candidate_ids):
    # A stable, persisted list bounds all joins and correlated quality checks.
    # LIMIT after the original global anti-joins still let PostgreSQL scan the
    # whole corpus; the ID bound must come before those eligibility checks.
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout='60s'")
        rows = conn.execute("""WITH ready AS MATERIALIZED (
          SELECT d.id,d.published_at,d.metadata->>'stock_code' stock_code
          FROM corpus.documents d JOIN ops.document_embedding_queue q ON q.document_id=d.id
          LEFT JOIN ops.document_embedding_model_state ms ON ms.document_id=d.id AND ms.embedding_model=%(model)s
          LEFT JOIN corpus.document_quality quality ON quality.document_id=d.id
          WHERE d.id=ANY(%(candidate_ids)s::uuid[]) AND q.status<>'running'
            AND coalesce(quality.quality_status,'accepted') IN ('accepted','short')
            AND (d.source_type<>'announcement' OR length(coalesce(nullif(d.content_text,''),nullif(d.content_md,''),d.raw_text,''))>=200)
            AND length(coalesce(nullif(d.content_text,''),nullif(d.content_md,''),nullif(d.raw_text,''),d.title,''))>0
            AND NOT EXISTS(SELECT 1 FROM corpus.document_quarantine x WHERE x.document_id=d.id AND x.released_at IS NULL)
            AND (coalesce(ms.retry_count,0)<3 OR ms.source_updated_at IS DISTINCT FROM d.updated_at)
            AND (ms.document_id IS NULL OR ms.status<>'ok' OR ms.source_updated_at IS DISTINCT FROM d.updated_at
              OR NOT EXISTS(SELECT 1 FROM corpus.document_embeddings_v2 e WHERE e.document_id=d.id AND e.embedding_model=%(model)s)
              OR EXISTS(SELECT 1 FROM corpus.document_embeddings_v2 e JOIN corpus.document_chunks c ON c.id=e.chunk_id
                WHERE e.document_id=d.id AND e.embedding_model=%(model)s AND c.metadata->>'document_truncated'='true'))
          ORDER BY d.published_at DESC NULLS LAST,d.id LIMIT %(scan_limit)s
        ), picked AS (
          SELECT q.document_id FROM ready r JOIN ops.document_embedding_queue q ON q.document_id=r.id
          WHERE q.status<>'running'
          ORDER BY EXISTS(SELECT 1 FROM agent.watchlist_items w WHERE w.removed_at IS NULL AND w.stock_code=r.stock_code) DESC,
            r.published_at DESC NULLS LAST,r.id
          FOR UPDATE OF q SKIP LOCKED LIMIT %(limit)s
        ), claimed AS (
          UPDATE ops.document_embedding_queue q SET status='running',started_at=clock_timestamp(),last_error=NULL
          FROM picked p WHERE q.document_id=p.document_id RETURNING q.document_id,q.started_at,q.source_updated_at
        ) SELECT d.id,d.title,d.source_type,d.source_name,d.updated_at,
          coalesce(nullif(d.content_text,''),nullif(d.content_md,''),d.raw_text,'') body,
          q.started_at AS lease_at,q.source_updated_at AS claimed_version
          FROM claimed q JOIN corpus.documents d ON d.id=q.document_id ORDER BY d.published_at DESC NULLS LAST,d.id
        """, {'model': model, 'limit': limit, 'scan_limit': len(candidate_ids),'candidate_ids':candidate_ids}).fetchall()
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO ops.document_embedding_model_state(document_id,embedding_model,source_updated_at,status)
              VALUES(%s,%s,%s,'running') ON CONFLICT(document_id,embedding_model) DO UPDATE SET
              source_updated_at=excluded.source_updated_at,status='running',updated_at=now(),
              retry_count=CASE WHEN ops.document_embedding_model_state.source_updated_at IS DISTINCT FROM excluded.source_updated_at
                THEN 0 ELSE ops.document_embedding_model_state.retry_count END""",
              [(r['id'],model,r['updated_at']) for r in rows])
    return rows


def release(conn, documents, model, error=None):
    """Touch only our exact leases; a changed/reclaimed source belongs to its new job."""
    with conn.transaction():
        for doc in documents:
            released = conn.execute("""UPDATE ops.document_embedding_queue SET status=%s,started_at=NULL,
              retry_count=retry_count+%s,last_error=%s WHERE document_id=%s AND status='running' AND started_at=%s RETURNING document_id""",
              ('failed' if error else 'pending',1 if error else 0,error,doc['id'],doc['lease_at'])).fetchone()
            if released:
                conn.execute("""UPDATE ops.document_embedding_model_state SET status=%s,retry_count=retry_count+%s,
                  last_error=%s,updated_at=now() WHERE document_id=%s AND embedding_model=%s AND status='running'""",
                  ('failed' if error else 'pending',1 if error else 0,error,doc['id'],model))


def store(conn, documents, worker, timings=None):
    started=time.monotonic()
    ids = [doc['id'] for doc in documents]
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout='120s'")
        conn.execute("SET LOCAL lock_timeout='8s'")
        current = {str(row['id']): row['updated_at'] for row in conn.execute(
            'SELECT id,updated_at FROM corpus.documents WHERE id=ANY(%s) ORDER BY id FOR UPDATE', (ids,)).fetchall()}
        leases = {str(row['document_id']): row for row in conn.execute(
            'SELECT document_id,status,started_at FROM ops.document_embedding_queue WHERE document_id=ANY(%s) ORDER BY document_id FOR UPDATE', (ids,)).fetchall()}
        valid = [doc for doc in documents if current.get(str(doc['id'])) == doc['updated_at']
            and leases.get(str(doc['id']), {}).get('status') == 'running'
            and leases[str(doc['id'])]['started_at'] == doc['lease_at']]
        valid_ids = [doc['id'] for doc in valid]
        if not valid:
            return []
        indexes = {str(row['document_id']): row['next_index'] for row in conn.execute(
            'SELECT document_id,max(chunk_index)+1 next_index FROM corpus.document_chunks WHERE document_id=ANY(%s) GROUP BY document_id', (valid_ids,)).fetchall()}
        # Replace this model only. Old BGE chunk references and vectors survive.
        conn.execute('DELETE FROM corpus.document_embeddings_v2 WHERE document_id=ANY(%s) AND embedding_model=%s', (valid_ids,worker.MODEL_NAME))
        conn.execute('''DELETE FROM corpus.document_chunks c WHERE c.document_id=ANY(%s)
          AND NOT EXISTS(SELECT 1 FROM corpus.document_embeddings e WHERE e.chunk_id=c.id)
          AND NOT EXISTS(SELECT 1 FROM corpus.document_embeddings_v2 e WHERE e.chunk_id=c.id)''', (valid_ids,))
        prepared_at=time.monotonic()
        chunk_rows, vector_rows = [], []
        for doc in valid:
            metadata = json.dumps({'chunker':'finance_heading_sentence_window_v2','source_type':doc['source_type'],
                'source_updated_at':str(doc['updated_at']),'content_sha256':doc['digest'],'document_truncated':False})
            vector_metadata = json.dumps({'normalized':True,'distance':'cosine','content_sha256':doc['digest']})
            for i,(text,vector) in enumerate(zip(doc['chunks'],doc['vectors'])):
                chunk_id = uuid.uuid4()
                chunk_rows.append((chunk_id,doc['id'],indexes.get(str(doc['id']),0)+i,text,worker.estimate_tokens(text),len(text),metadata))
                vector_rows.append((doc['id'],chunk_id,worker.MODEL_NAME,worker.vector_literal(vector),worker.MODEL_DIMENSIONS,vector_metadata))
        with conn.cursor() as cur:
            with cur.copy('COPY corpus.document_chunks(id,document_id,chunk_index,chunk_text,token_count,char_count,metadata) FROM STDIN') as copy:
                for row in chunk_rows: copy.write_row(row)
            chunks_at=time.monotonic()
            with cur.copy('COPY corpus.document_embeddings_v2(document_id,chunk_id,embedding_model,embedding,dimensions,metadata) FROM STDIN') as copy:
                for row in vector_rows: copy.write_row(row)
            vectors_at=time.monotonic()
            cur.executemany("""UPDATE ops.document_embedding_model_state SET status='ok',source_updated_at=%s,
              content_sha256=%s,retry_count=0,last_error=NULL,updated_at=now() WHERE document_id=%s AND embedding_model=%s""",
              [(d['updated_at'],d['digest'],d['id'],worker.MODEL_NAME) for d in valid])
            cur.executemany("""UPDATE ops.document_embedding_queue SET status='ok',source_updated_at=%s,processed_at=now(),
              embedding_model=%s,chunk_count=%s,retry_count=0,last_error=NULL,started_at=NULL WHERE document_id=%s""",
              [(d['updated_at'],worker.MODEL_NAME,len(d['chunks']),d['id']) for d in valid])
            cur.executemany("""UPDATE corpus.document_quality SET checks=checks||jsonb_build_object(
              'embedding_chunker','finance_heading_sentence_window_v2','embedding_chunk_count',%s,'embedding_truncated',false),
              checked_at=greatest(checked_at,now()) WHERE document_id=%s""", [(len(d['chunks']),d['id']) for d in valid])
        state_at=time.monotonic()
    if timings is not None:
        timings.update({name:round(seconds,3) for name,seconds in {
            'lock_and_replace':prepared_at-started,'chunks_copy':chunks_at-prepared_at,
            'vectors_copy':vectors_at-chunks_at,'state_updates':state_at-vectors_at,
            'commit':time.monotonic()-state_at}.items()})
    return valid_ids


def save(path, value):
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')
    temp.replace(path)


def snapshot(conn, path):
    with conn.transaction():
        conn.execute("SET LOCAL statement_timeout='60s'")
        ids=[str(row['id']) for row in conn.execute(
            'SELECT id FROM corpus.documents ORDER BY published_at DESC NULLS LAST,id').fetchall()]
    save(path,ids)
    return ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--documents-per-batch',type=int,default=64)
    parser.add_argument('--batch-size',type=int,default=128)
    parser.add_argument('--chunk-budget',type=int,default=1024)
    parser.add_argument('--max-documents',type=int,default=0,help='0 runs until no eligible work remains')
    parser.add_argument('--max-seconds',type=int,default=0,help='0 has no time limit')
    args=parser.parse_args()
    if not 1<=args.documents_per_batch<=256 or not 1<=args.batch_size<=128 or not 128<=args.chunk_budget<=4096:
        parser.error('batch parameters outside bounded limits')
    os.environ.update(load_env(ROOT/'.env.finance-agent-v3'))
    import sync_document_embeddings as worker
    from urllib.parse import urlparse
    assert worker.EMBEDDING_PROVIDER=='openai_compatible' and worker.MODEL_DIMENSIONS==1024
    assert worker.MODEL_NAME=='Qwen/Qwen3-Embedding-0.6B'
    assert urlparse(worker.EMBEDDING_BASE_URL).hostname in ('127.0.0.1','localhost'), 'local embedding gateway only'
    args.state_dir.mkdir(parents=True,exist_ok=True)
    lock=(args.state_dir/'worker.lock').open('w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    stopping=False
    def stop(_signum,_frame):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    start=time.monotonic()
    run_id=str(uuid.uuid4())
    counters={'processed':0,'chunks':0,'failed':0,'deferred_source_changes':0,'batches':0}
    active=[]
    status='running'
    conn=connect();conn.autocommit=True
    lease_file=args.state_dir/'leases.json'
    if lease_file.exists():
        old=json.loads(lease_file.read_text())
        for d in old:
            d['id']=uuid.UUID(d['id']);d['lease_at']=dt.datetime.fromisoformat(d['lease_at'])
        release(conn,old,worker.MODEL_NAME)
    worklist_path=args.state_dir/'worklist.json'
    cursor_path=args.state_dir/'cursor.json'
    previous_path=args.state_dir/'progress.json'
    previous=json.loads(previous_path.read_text()) if previous_path.exists() else {}
    new_pass=previous.get('status')=='no_eligible_work'
    worklist=json.loads(worklist_path.read_text()) if worklist_path.exists() and not new_pass else snapshot(conn,worklist_path)
    saved_cursor=json.loads(cursor_path.read_text()) if cursor_path.exists() and not new_pass else {}
    cursor=saved_cursor.get('cursor',0)
    pass_processed=saved_cursor.get('pass_processed',0)
    run_log=(args.state_dir/'batches.jsonl').open('a',buffering=1)
    def progress(extra=None):
        value={**counters,'run_id':run_id,'status':status,'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'elapsed_seconds':round(time.monotonic()-start,3),'documents_per_batch':args.documents_per_batch,
            'request_batch_size':args.batch_size,**(extra or {})}
        save(args.state_dir/'progress.json',value)
        run_log.write(json.dumps(value,ensure_ascii=False,default=str)+'\n')
        print(json.dumps(value,ensure_ascii=False,default=str),flush=True)
    try:
        while not stopping:
            if args.max_seconds and time.monotonic()-start>=args.max_seconds:
                status='time_budget_reached';break
            if args.max_documents and counters['processed']>=args.max_documents:
                status='document_budget_reached';break
            if cursor>=len(worklist):
                if pass_processed==0:
                    status='no_eligible_work';break
                worklist=snapshot(conn,worklist_path)
                cursor=pass_processed=0
                save(cursor_path,{'cursor':cursor,'pass_processed':pass_processed})
            started=time.monotonic()
            count=min(args.documents_per_batch,args.max_documents-counters['processed']) if args.max_documents else args.documents_per_batch
            candidates=worklist[cursor:cursor+count]
            active=claim(conn,count,worker.MODEL_NAME,candidates)
            save(lease_file,[{'id':d['id'],'lease_at':d['lease_at']} for d in active])
            claim_seconds=time.monotonic()-started
            if not active:
                cursor+=len(candidates)
                save(cursor_path,{'cursor':cursor,'pass_processed':pass_processed})
                continue
            documents=[]
            for row in active:
                try:documents.append(prepare(row,worker))
                except ValueError as exc:
                    release(conn,[row],worker.MODEL_NAME,str(exc));counters['failed']+=1
            for group in groups(documents,args.chunk_budget):
                if stopping:break
                inference_start=time.monotonic()
                texts=[text for doc in group for text in doc['chunks']]
                vectors=worker.remote_embed(texts,args.batch_size)
                assign_vectors(group,vectors,worker.MODEL_DIMENSIONS)
                inference_seconds=time.monotonic()-inference_start
                write_start=time.monotonic()
                store_timings={}
                saved=store(conn,group,worker,store_timings)
                saved_set=set(saved)
                release(conn,[d for d in group if d['id'] not in saved_set],worker.MODEL_NAME)
                counters['processed']+=len(saved)
                pass_processed+=len(saved)
                counters['chunks']+=sum(len(d['chunks']) for d in group if d['id'] in saved_set)
                counters['deferred_source_changes']+=len(group)-len(saved)
                counters['batches']+=1
                progress({'claim_seconds':round(claim_seconds,3),'inference_seconds':round(inference_seconds,3),
                    'store_seconds':round(time.monotonic()-write_start,3),'batch_documents':len(saved),'batch_chunks':len(texts)})
                save(args.state_dir/'latest-store-timing.json',store_timings)
                # Release large float arrays before preparing the next bundle.
                for doc in group:doc.pop('vectors',None)
                del vectors
            release(conn,active,worker.MODEL_NAME)
            active=[];save(lease_file,[])
            if not stopping:
                cursor+=len(candidates)
                save(cursor_path,{'cursor':cursor,'pass_processed':pass_processed})
            if counters['batches'] and counters['batches']%50==0:worker.refresh_embedding_watermark()
        if stopping:status='stopped_at_batch_boundary'
    except Exception as exc:
        status='error'
        progress({'error':f'{type(exc).__name__}: {exc}'[:1000]})
        raise
    finally:
        release(conn,active,worker.MODEL_NAME)
        save(lease_file,[])
        conn.close()
        worker.refresh_embedding_watermark()
        progress()
        run_log.close()
    return 0 if counters['failed']==0 else 1


if __name__=='__main__':raise SystemExit(main())
