"""Bounded, idempotent persistence for rule extraction; preserve superseded evidence."""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import psycopg
from common import connect, jsonb
from graph_revision_policy import GRAPH_WRITE_LOCK

STORE_VERSION = 'rules-store:v1'
INPUT_FIELDS = ('document_id', 'source_type', 'source_name', 'title', 'event_date',
                'content_text', 'content_md', 'raw_text', 'stock_code', 'stock_name',
                'report_type', 'industry_name', 'report_date')
EVENT_FIELDS = ('event_date', 'event_type', 'polarity', 'impact_score', 'confidence',
                'entity_texts', 'asset_ids', 'stock_codes', 'industries', 'keywords',
                'summary', 'evidence', 'metadata')

CANDIDATE_SQL = """
-- Materialize cheap date/version predicates before detoasting document bodies.
-- The original join let the planner read every announcement body before dates.
WITH candidate_ids AS MATERIALIZED (
 SELECT document_id AS id FROM research.reports_resolved
 WHERE (%(since)s::date IS NULL OR report_date >= %(since)s::date)
   AND (%(until)s::date IS NULL OR report_date <= %(until)s::date)
   AND (%(document_ids)s::uuid[] IS NULL OR document_id=ANY(%(document_ids)s::uuid[]))
 UNION
 SELECT id FROM corpus.documents
 WHERE (%(since)s::date IS NULL OR published_at >= (%(since)s::date::timestamp AT TIME ZONE 'Asia/Shanghai'))
   AND (%(until)s::date IS NULL OR published_at < ((%(until)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai'))
   AND (%(document_ids)s::uuid[] IS NULL OR id=ANY(%(document_ids)s::uuid[]))
 UNION
 SELECT id FROM corpus.documents WHERE source_type='announcement'
   AND updated_at>=now()-make_interval(hours=>%(updated_hours)s)
   AND (%(document_ids)s::uuid[] IS NULL OR id=ANY(%(document_ids)s::uuid[]))
), dated AS MATERIALIZED (
 SELECT d.id, greatest(d.updated_at,r.updated_at,r.identity_known_at) AS source_updated_at, coalesce(r.report_date,(d.published_at AT TIME ZONE 'Asia/Shanghai')::date) AS event_date
 FROM candidate_ids candidates JOIN corpus.documents d ON d.id=candidates.id
 LEFT JOIN research.reports_resolved r ON r.document_id=d.id
 WHERE (%(source_types)s::text[] IS NULL OR d.source_type=ANY(%(source_types)s))
  AND (%(document_ids)s::uuid[] IS NULL OR d.id=ANY(%(document_ids)s::uuid[]))
  AND (%(since)s::date IS NULL OR coalesce(r.report_date,(d.published_at AT TIME ZONE 'Asia/Shanghai')::date)>=%(since)s::date
       OR (d.source_type='announcement' AND d.updated_at>=now()-make_interval(hours=>%(updated_hours)s)))
  AND (%(until)s::date IS NULL OR coalesce(r.report_date,(d.published_at AT TIME ZONE 'Asia/Shanghai')::date)<=%(until)s::date)

), eligible AS MATERIALIZED (
 SELECT d.id,d.event_date FROM dated d
 WHERE NOT EXISTS(SELECT 1 FROM corpus.document_quarantine x WHERE x.document_id=d.id AND x.released_at IS NULL)
  AND (%(force)s OR NOT EXISTS(SELECT 1 FROM kg.document_extraction_status s
       WHERE s.document_id=d.id AND s.extraction_method=%(method)s AND s.status IN ('ok','skipped')
         AND s.source_updated_at>=d.source_updated_at
         AND s.details->>'logic_version'=%(logic_version)s))
)
-- Load bodies only for this bounded batch. Short announcements are explicitly
-- recorded as skipped by run(); a later body version makes them eligible again.
-- Previously checking length here detoasted >75k bodies just to choose 10k IDs.
SELECT c.id::text AS document_id FROM eligible c
ORDER BY c.event_date DESC NULLS LAST,c.id
LIMIT %(limit)s
"""
LOAD_SQL = """
SELECT d.id::text AS document_id,d.source_type,d.source_name,d.title,
 (d.published_at AT TIME ZONE 'Asia/Shanghai')::date AS event_date,
 d.content_text,d.content_md,d.raw_text,
 coalesce(r.stock_code,d.metadata->>'stock_code') AS stock_code,
 coalesce(r.stock_name,d.metadata->>'stock_name') AS stock_name,
 r.report_type,r.industry_name,r.report_date,
 d.updated_at AS document_updated_at,greatest(r.updated_at,r.identity_known_at) AS report_updated_at,
 greatest(d.updated_at,r.updated_at,r.identity_known_at) AS source_updated_at
FROM corpus.documents d LEFT JOIN research.reports_resolved r ON r.document_id=d.id
WHERE d.id=ANY(%s::uuid[]) ORDER BY d.id
"""
# Cast matches the existing partial index; avoid scanning 1.5 million graph rows per document.
ARCHIVE_GRAPH_SQL = """
UPDATE kg.relation_versions SET status='archived',updated_at=now(),
 attributes=attributes||jsonb_build_object('source_invalidation',%s::text,
  'source_event_revision_id',%s::text,'source_revision_recorded_at',now())
WHERE extractor_version='temporal_events:v1'
 AND source_system='analytics.document_events'
 AND (attributes->>'event_id')::uuid=%s::uuid AND status<>'archived'
"""


class RetryDocument(RuntimeError):
    """Source changed or another writer holds the required short transaction lock."""


def input_signature(row, method, logic_version, rule_fingerprint):
    value = {k: row.get(k) for k in INPUT_FIELDS}
    value.update(method=method, logic_version=logic_version, rule_fingerprint=rule_fingerprint)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def rule_signature(rules, industries, attribution_version):
    value = {'rules': [asdict(r) for r in rules], 'industries': industries, 'attribution': attribution_version}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def ensure_schema():
    # Read the packaged migration only if missing; ordinary runs do not require admin.
    with connect() as conn:
        exists = conn.execute("SELECT to_regclass('analytics.document_rule_revisions') IS NOT NULL AS ok").fetchone()['ok']
    if not exists:
        path = Path(__file__).resolve().parents[2] / 'sql' / 'document_rule_revisions_v1.sql'
        with connect(autocommit=False, admin=True) as conn:
            conn.execute("SET LOCAL statement_timeout='25s'")
            conn.execute("SET LOCAL lock_timeout='3s'")
            conn.execute(path.read_text())


def set_limits(conn):
    conn.execute("SET LOCAL statement_timeout='25s'")
    conn.execute("SET LOCAL lock_timeout='2s'")


def lock_current_source(cur, row):
    if not cur.execute("SELECT pg_try_advisory_xact_lock(hashtextextended(%s,704201314)) AS ok",
                       ('rules-document:' + row['document_id'],)).fetchone()['ok']:
        raise RetryDocument('document extraction lock busy')
    current = cur.execute("""SELECT d.updated_at AS document_updated_at,greatest(r.updated_at,r.identity_known_at) AS report_updated_at,
        EXISTS(SELECT 1 FROM corpus.document_quarantine q WHERE q.document_id=d.id AND q.released_at IS NULL) AS quarantined
        FROM corpus.documents d LEFT JOIN research.reports_resolved r ON r.document_id=d.id
        WHERE d.id=%s FOR SHARE OF d""", (row['document_id'],)).fetchone()
    if not current or current['quarantined']:
        raise RetryDocument('document absent or quarantined')
    if any(current[k] != row[k] for k in ('document_updated_at', 'report_updated_at')):
        raise RetryDocument('source changed after bounded read')
    # The captured report timestamp is written, never a fresh later version. A concurrent
    # report-only update will remain eligible on the next run even without locking reports.


def canonical_asset_ids(cur, codes):
    return [str(r['asset_id']) for r in cur.execute("""SELECT asset_id FROM market.assets
        WHERE asset_type='stock' AND symbol=ANY(%s::text[]) AND NOT(metadata?'identity_alias_of')
        ORDER BY asset_id""", (codes,)).fetchall()]


def normal_event(event):
    out = dict(event)
    out['asset_ids'] = sorted(str(v) for v in out.get('asset_ids', []))
    return out


def changed_event(old, desired):
    return any(normal_event(old).get(k) != normal_event(desired).get(k) for k in EVENT_FIELDS)


def archive_event(cur, old, signature, reason):
    revision = cur.execute("""INSERT INTO analytics.document_rule_revisions
        (document_id,extraction_method,object_type,source_object_id,reason,input_signature,previous_row)
        SELECT document_id,extraction_method,'event',id::text,%s,%s,to_jsonb(e)
        FROM analytics.document_events e WHERE id=%s RETURNING revision_id""",
        (reason, signature, old['id'])).fetchone()['revision_id']
    return cur.execute(ARCHIVE_GRAPH_SQL, (reason, str(revision), old['id'])).rowcount


def persist_document(cur, row, events, keywords, method, logic_version, signature, catalog_signature, force=False):
    lock_current_source(cur, row)
    stats = Counter(documents=1)
    old_status = cur.execute("""SELECT * FROM kg.document_extraction_status
        WHERE document_id=%s AND extraction_method=%s FOR UPDATE""", (row['document_id'], method)).fetchone()
    same_input = bool(old_status and old_status['status'] in ('ok', 'skipped')
                      and old_status['details'].get('input_signature') == signature
                      and old_status['details'].get('store_version') == STORE_VERSION)
    if same_input:
        stats['unchanged_inputs'] += 1
    if same_input and not force:
        event_count, keyword_count = old_status['event_count'], old_status['keyword_count']
        details = old_status['details']
    else:
        desired = {}
        for event in events:
            event = dict(event)
            event['asset_ids'] = canonical_asset_ids(cur, event['stock_codes'])
            desired[event['event_hash']] = event
        existing = cur.execute("""SELECT * FROM analytics.document_events
            WHERE document_id=%s AND extraction_method=%s FOR UPDATE""", (row['document_id'], method)).fetchall()
        invalidated = [e for e in existing if e['event_hash'] not in desired or changed_event(e, desired[e['event_hash']])]
        if invalidated:
            if not cur.execute('SELECT pg_try_advisory_xact_lock(%s) AS ok', (GRAPH_WRITE_LOCK,)).fetchone()['ok']:
                raise RetryDocument('graph revision writer busy; no source outputs changed')
            for old in invalidated:
                reason = 'rule_input_replaced' if old['event_hash'] in desired else 'rule_no_longer_supported'
                stats['graph_versions_archived'] += archive_event(cur, old, signature, reason)
                stats['event_revisions'] += 1
            obsolete = [e['id'] for e in invalidated if e['event_hash'] not in desired]
            if obsolete:
                stats['events_deleted'] += cur.execute('DELETE FROM analytics.document_events WHERE id=ANY(%s::uuid[])', (obsolete,)).rowcount
        old_by_hash = {e['event_hash']: e for e in existing}
        for event in desired.values():
            old = old_by_hash.get(event['event_hash'])
            if old and not changed_event(old, event):
                continue
            values = {**event, 'metadata': jsonb(event['metadata'])}
            stats['events_written'] += cur.execute("""INSERT INTO analytics.document_events
                (document_id,event_date,event_type,polarity,impact_score,confidence,entity_texts,asset_ids,
                 stock_codes,industries,keywords,summary,evidence,extraction_method,event_hash,metadata,updated_at)
                VALUES(%(document_id)s,%(event_date)s,%(event_type)s,%(polarity)s,%(impact_score)s,%(confidence)s,
                 %(entity_texts)s,%(asset_ids)s::uuid[],%(stock_codes)s,%(industries)s,%(keywords)s,%(summary)s,
                 %(evidence)s,%(extraction_method)s,%(event_hash)s,%(metadata)s,now())
                ON CONFLICT(event_hash) DO UPDATE SET event_date=excluded.event_date,event_type=excluded.event_type,
                 polarity=excluded.polarity,impact_score=excluded.impact_score,confidence=excluded.confidence,
                 entity_texts=excluded.entity_texts,asset_ids=excluded.asset_ids,stock_codes=excluded.stock_codes,
                 industries=excluded.industries,keywords=excluded.keywords,summary=excluded.summary,
                 evidence=excluded.evidence,metadata=excluded.metadata,updated_at=now()""", values).rowcount
        wanted = dict(keywords)
        existing_keywords = cur.execute('SELECT * FROM corpus.document_keywords WHERE document_id=%s AND method=%s FOR UPDATE',
                                        (row['document_id'], method)).fetchall()
        for old in existing_keywords:
            if old['keyword'] not in wanted or float(old['weight']) != wanted[old['keyword']]:
                cur.execute("""INSERT INTO analytics.document_rule_revisions
                    (document_id,extraction_method,object_type,source_object_id,reason,input_signature,previous_row)
                    SELECT document_id,method,'keyword',id::text,%s,%s,to_jsonb(k)
                    FROM corpus.document_keywords k WHERE id=%s""",
                    ('rule_no_longer_supported' if old['keyword'] not in wanted else 'rule_weight_revised', signature, old['id']))
                stats['keyword_revisions'] += 1
        stats['keywords_deleted'] += cur.execute("""DELETE FROM corpus.document_keywords
            WHERE document_id=%s AND method=%s AND NOT(keyword=ANY(%s::text[]))""", (row['document_id'], method, list(wanted))).rowcount
        for keyword, weight in keywords:
            stats['keywords_written'] += cur.execute("""INSERT INTO corpus.document_keywords(document_id,keyword,weight,method)
                VALUES(%s,%s,%s,%s) ON CONFLICT(document_id,keyword,method) DO UPDATE SET weight=excluded.weight
                WHERE corpus.document_keywords.weight IS DISTINCT FROM excluded.weight""", (row['document_id'], keyword, weight, method)).rowcount
        event_count, keyword_count = len(desired), len(wanted)
        details = {'extractor': 'rules', 'version': method, 'logic_version': logic_version,
                   'store_version': STORE_VERSION, 'input_signature': signature,
                   'resolver_catalog_signature': catalog_signature}
    status = 'skipped' if row.get('_skip_reason') else 'ok'
    if row.get('_skip_reason'):
        details = {**details, 'skip_reason': row['_skip_reason']}
    # Replaying the same input issues no event/keyword/status DML (row locks can still log WAL).
    stats['status_written'] += cur.execute("""INSERT INTO kg.document_extraction_status
        (document_id,extraction_method,status,event_count,keyword_count,processed_at,source_updated_at,details)
        VALUES(%s,%s,%s,%s,%s,now(),%s,%s)
        ON CONFLICT(document_id,extraction_method) DO UPDATE SET status=excluded.status,event_count=excluded.event_count,
         keyword_count=excluded.keyword_count,processed_at=now(),source_updated_at=excluded.source_updated_at,details=excluded.details
        WHERE (kg.document_extraction_status.status,kg.document_extraction_status.event_count,
               kg.document_extraction_status.keyword_count,kg.document_extraction_status.source_updated_at,
               kg.document_extraction_status.details)
          IS DISTINCT FROM (excluded.status,excluded.event_count,excluded.keyword_count,excluded.source_updated_at,excluded.details)""",
        (row['document_id'], method, status, event_count, keyword_count, row['source_updated_at'], jsonb(details))).rowcount
    stats.update(events=event_count, keywords=keyword_count)
    return stats


RETRYABLE = (RetryDocument, psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable,
             psycopg.errors.SerializationFailure, psycopg.errors.QueryCanceled)


def commit_batch(conn, prepared, args, catalog_signature):
    """Rollback the whole bounded batch on contention; caller retries individual docs."""
    result = Counter()
    try:
        set_limits(conn)
        with conn.cursor() as cur:
            for row, events, keywords, signature in sorted(prepared, key=lambda x: x[0]['document_id']):
                result.update(persist_document(cur, row, events, keywords, args.method, args.logic_version, signature, catalog_signature, force=args.force))
        conn.commit()
        result['commits'] += 1
        return result
    except Exception:
        conn.rollback()
        raise


def run(args, source_types, extract, resolver_cls, rules, industries, attribution_version):
    if not 1 <= args.batch_size <= 50 or args.limit < 1:
        raise ValueError('batch size must be 1..50 and limit positive')
    ensure_schema()
    fingerprint = rule_signature(rules, industries, attribution_version)
    totals = Counter()
    errors = []
    started = time.monotonic()
    with connect(autocommit=False) as conn:
        set_limits(conn)
        params = dict(source_types=source_types, document_ids=args.document_id, since=args.since,
                      until=args.until, updated_hours=max(0,args.include_updated_announcements_hours),
                      force=args.force, method=args.method, logic_version=args.logic_version, limit=args.limit)
        candidates = [r['document_id'] for r in conn.execute(CANDIDATE_SQL, params).fetchall()]
        args.last_candidate_count = len(candidates)
        companies = conn.execute("""SELECT symbol,name FROM market.assets WHERE asset_type='stock'
            AND name IS NOT NULL AND NOT(metadata?'identity_alias_of') ORDER BY symbol,name""").fetchall()
        conn.commit()
        resolver = resolver_cls(companies)
        catalog_signature = hashlib.sha256(json.dumps(companies, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for offset in range(0, len(candidates), args.batch_size):
            set_limits(conn)
            rows = conn.execute(LOAD_SQL, (candidates[offset:offset+args.batch_size],)).fetchall()
            conn.commit()
            prepared = []
            for row in rows:
                body = row.get('content_text') or row.get('content_md') or row.get('raw_text') or ''
                if row['source_type'] == 'announcement' and len(body) < 200:
                    row['_skip_reason'] = 'announcement_body_too_short'
                    outputs = ([], [])
                else:
                    outputs = extract(row, args.method, resolver)
                prepared.append((row, *outputs, input_signature(row, args.method, args.logic_version, fingerprint)))
            loaded_ids = {row['document_id'] for row in rows}
            errors.extend({'document_id': doc, 'error': 'candidate deleted before bounded load', 'retryable': True}
                          for doc in candidates[offset:offset+args.batch_size] if doc not in loaded_ids)
            try:
                totals.update(commit_batch(conn, prepared, args, catalog_signature))
            except RETRYABLE:
                totals['batch_contention_fallbacks'] += 1
                for item in prepared:
                    for attempt in range(2):
                        try:
                            totals.update(commit_batch(conn, [item], args, catalog_signature))
                            break
                        except RETRYABLE as exc:
                            if attempt == 1:
                                errors.append({'document_id': item[0]['document_id'], 'error': str(exc).splitlines()[0], 'retryable': True})
                            else:
                                time.sleep(.1)
                        except psycopg.Error as exc:
                            errors.append({'document_id': item[0]['document_id'], 'error_type': type(exc).__name__, 'sqlstate': exc.sqlstate, 'retryable': False})
                            break
            except psycopg.Error as exc:
                # Log all rolled-back IDs without SQL/body fragments or credentials.
                errors.extend({'document_id': x[0]['document_id'], 'error_type': type(exc).__name__, 'sqlstate': exc.sqlstate, 'retryable': False} for x in prepared)
            if offset == 0 or (offset + args.batch_size) % 500 == 0:
                print(json.dumps({'candidate_progress': min(offset+args.batch_size,len(candidates)), 'candidates': len(candidates), **totals, 'failures': len(errors)}), flush=True)
    report = {'store_version': STORE_VERSION, 'candidates': len(candidates), **totals, 'failures': errors,
              'elapsed_seconds': round(time.monotonic()-started,3), 'batch_size': args.batch_size,
              'status': 'complete' if not errors else 'incomplete_retry_required'}
    if args.report_json:
        Path(args.report_json).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if not errors else 2
