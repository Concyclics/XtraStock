#!/usr/bin/env python3
"""Classify traceable source claims; never promote them to verified business facts.

Only current research-report relations and attributed company exposures have
review rules. Other extractors remain unreviewed until their own rules exist.
"""
from __future__ import annotations
import argparse
from collections import Counter
import datetime as dt
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo
from common import connect, repo_root, jsonb
from graph_revision_policy import GRAPH_WRITE_LOCK

CONTRACT = 'source-provenance-v1'
SCOPE = """v.valid_during @> %(as_of)s::date
 AND v.source_system IN ('research.reports','kg.company_concept_exposures')
 AND v.review_status IN ('unreviewed','classified')"""
BRIDGE_SQL = """
INSERT INTO kg.relation_version_evidence
 (relation_version_id,document_id,public_at,system_at,evidence_text,confidence,evidence_hash,attributes)
SELECT v.relation_version_id,d.id,d.published_at,clock_timestamp(),x.evidence,x.confidence,
 md5(v.relation_version_id::text||':source:'||d.id::text),
 jsonb_build_object('source_name',d.source_name,'source_type',d.source_type,'exposure_id',x.exposure_id)
FROM kg.relation_versions v
JOIN kg.company_concept_exposures x ON x.exposure_id::text=v.attributes->>'exposure_id'
JOIN corpus.documents d ON d.id=x.evidence_document_id
WHERE v.source_system='kg.company_concept_exposures'
 AND v.valid_during @> %(as_of)s::date AND v.status='active' AND x.status='active'
 AND x.metadata->'attribution_check'->>'supported'='true'
 AND x.review_status NOT IN ('rejected','disputed')
 AND nullif(trim(x.evidence),'') IS NOT NULL
ON CONFLICT (relation_version_id,evidence_hash) DO UPDATE SET
 public_at=EXCLUDED.public_at,evidence_text=EXCLUDED.evidence_text,
 confidence=EXCLUDED.confidence,attributes=EXCLUDED.attributes
"""
SELECT_SQL = """
SELECT v.relation_version_id,v.source_system,v.extractor_version,v.status,v.review_status,
 v.attributes,v.public_known_during::text,v.valid_during::text,r.relation_type,
 e.document_id,e.evidence_text,e.public_at,e.system_at,e.attributes AS evidence_attributes,
 d.title,d.source_type AS document_type,d.source_name AS publisher,d.published_at,
 NOT EXISTS (SELECT 1 FROM corpus.document_quarantine q WHERE q.document_id=d.id AND q.released_at IS NULL) AS source_readable,
 CASE WHEN length(e.evidence_text)>20 THEN
   position(regexp_replace(e.evidence_text,'\\s+','','g') in
     regexp_replace(coalesce(nullif(d.content_text,''),nullif(d.content_md,''),d.raw_text,''),'\\s+','','g'))>0
 ELSE false END AS text_matches_document,
 CASE WHEN v.source_system='research.reports' THEN EXISTS (
   SELECT 1 FROM research.reports_resolved p WHERE p.document_id=e.document_id
    AND p.stock_code=v.attributes->>'stock_code' AND trim(p.institution)=v.attributes->>'institution'
 ) WHEN v.source_system='kg.company_concept_exposures' THEN EXISTS (
   SELECT 1 FROM kg.company_concept_exposures x
    WHERE x.exposure_id::text=v.attributes->>'exposure_id'
     AND x.evidence_document_id=e.document_id AND x.stock_code=v.attributes->>'stock_code'
     AND x.status='active' AND x.review_status NOT IN ('rejected','disputed')
     AND x.metadata->'attribution_check'->>'supported'='true' AND x.evidence=e.evidence_text
 ) ELSE false END AS source_record_matches
FROM kg.relation_versions v JOIN kg.relations r USING(relation_id)
LEFT JOIN LATERAL (
 SELECT e.* FROM kg.relation_version_evidence e
 WHERE e.relation_version_id=v.relation_version_id AND e.document_id::text=v.attributes->>'document_id'
 ORDER BY e.public_at DESC NULLS LAST,e.evidence_id DESC LIMIT 1
) e ON true
LEFT JOIN corpus.documents d ON d.id=e.document_id
WHERE """+SCOPE+" ORDER BY v.relation_version_id LIMIT %(limit)s"


def classify(record, now):
    attrs = record.get('attributes') or {}
    reason = 'traceable_source_claim'
    if record['status'] != 'active': reason='inactive_relation'
    elif attrs.get('superseded_by_canonical') is True: reason='superseded_relation'
    elif (attrs.get('attribution_check') or {}).get('supported') is False: reason='rejected_attribution'
    elif not record.get('document_id') or str(record['document_id']) != attrs.get('document_id'): reason='missing_source_document'
    elif not record.get('title') or not record.get('document_type') or not record.get('publisher'): reason='missing_source_identity'
    elif not record.get('public_at') or not record.get('published_at') or record['public_at'] > now or record['published_at'] > now: reason='missing_or_future_public_time'
    elif not record.get('system_at') or record['system_at'] > now: reason='not_yet_available'
    elif not record.get('source_readable',True): reason='quarantined_source'
    elif not record.get('source_record_matches'): reason='source_record_mismatch'
    elif not record.get('text_matches_document'): reason='source_text_mismatch'
    elif record['source_system']=='research.reports' and (record.get('document_type')!='research_report' or record['relation_type'] not in {'COVERS_COMPANY','RATES','FORECASTS','IN_INDUSTRY'}): reason='unsupported_research_relation'
    elif record['source_system']=='kg.company_concept_exposures' and (record['relation_type']!='EXPOSED_TO' or (attrs.get('attribution_check') or {}).get('supported') is not True): reason='missing_positive_attribution'
    elif record['source_system'] not in {'research.reports','kg.company_concept_exposures'}: reason='no_review_rule'
    # Hash only source inputs; review metadata cannot invalidate its own hash.
    source={k:v for k,v in record.items() if k not in {'review_status','attributes','system_at','source_readable'}}
    source['attributes']={k:v for k,v in attrs.items() if k != 'evidence_review'}
    fingerprint=hashlib.sha256(json.dumps(source,sort_keys=True,default=str,ensure_ascii=False).encode()).hexdigest()
    return {'contract':CONTRACT,'eligible':reason=='traceable_source_claim','reason':reason,
            'source_fingerprint':fingerprint,'claim_class':'extracted_claim',
            'meaning':'Source identity, time, text and subject linkage checked; business realization not verified.'}


def review_current(conn, *, as_of=None, apply=False, max_rows=20000, audit=None):
    now=dt.datetime.now(dt.timezone.utc)
    as_of=as_of or now.astimezone(ZoneInfo('Asia/Shanghai')).date()
    params={'as_of':as_of,'limit':max_rows+1}
    if not conn.execute('SELECT pg_try_advisory_xact_lock(%s) acquired',(GRAPH_WRITE_LOCK,)).fetchone()['acquired']:
        raise RuntimeError('another graph writer owns the transaction lock')
    if not conn.execute("SELECT to_regclass('kg.version_revision_history') name").fetchone()['name']:
        raise RuntimeError('immutable revision history is required')
    bridged=conn.execute(BRIDGE_SQL,params).rowcount if apply else 0
    records=conn.execute(SELECT_SQL,params).fetchall()
    if len(records)>max_rows:raise RuntimeError('review scope exceeds max_rows; no partial classification committed')
    now=dt.datetime.now(dt.timezone.utc)
    changes, failures, reasons=[],[],Counter()
    for record in records:
        decision=classify(record,now);reasons[decision['reason']]+=1
        target='classified' if decision['eligible'] else 'unreviewed'
        if not decision['eligible']:
            failures.append({"relation_version_id":str(record['relation_version_id']),
                             "document_id":str(record['document_id']) if record.get('document_id') else None,
                             "source_system":record['source_system'],"reason":decision['reason']})
        old=(record.get('attributes') or {}).get('evidence_review')
        # Keep an audit of failed checks in this run without churning every
        # unreviewed row in the database. Revoke obsolete auto-classifications.
        if (decision['eligible'] or record['review_status']=='classified') and (record['review_status']!=target or old!=decision):
            changes.append({'relation_version_id':str(record['relation_version_id']),
                            'before':record,'after_review_status':target,'after_review':decision})
    if audit is not None:
        audit.mkdir(parents=True,exist_ok=True)
        (audit/'review-failures.json').write_text(json.dumps(failures,ensure_ascii=False,indent=2)+'\n')
        (audit/'review-changes.json').write_text(json.dumps(changes,ensure_ascii=False,default=str,indent=2)+'\n')
    if apply:
        with conn.cursor() as cur:
            cur.executemany("""UPDATE kg.relation_versions SET review_status=%s,
                attributes=attributes||jsonb_build_object('evidence_review',%s::jsonb),updated_at=clock_timestamp()
                WHERE relation_version_id=%s AND review_status IN ('unreviewed','classified')""",
                [(r['after_review_status'],jsonb(r['after_review']),r['relation_version_id']) for r in changes])
    summary={'mode':'applied' if apply else 'dry_run','as_of':str(as_of),'rows_checked':len(records),
             'evidence_links_written':bridged,'classification_changes':len(changes),'reasons':dict(reasons),
             'contract':CONTRACT,'auto_approved_business_facts':0}
    if audit is not None:(audit/'review-summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true')
    p.add_argument('--output',type=Path,required=True);p.add_argument('--as-of');p.add_argument('--max-rows',type=int,default=20000)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    with connect(admin=args.apply,autocommit=True,attempts=1) as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(hashtextextended('finance-pipeline:knowledge_graph',0)) acquired").fetchone()['acquired']:
            raise RuntimeError('knowledge graph pipeline is busy; no changes made')
        try:
            if args.apply:
                # Release the schema lock before evidence/source processing.
                with conn.transaction():
                    conn.execute("SET LOCAL lock_timeout='5s'")
                    conn.execute("SET LOCAL statement_timeout='120s'")
                    conn.execute((repo_root()/'docker/postgres/migrate_graph_source_review_v6.sql').read_text())
            with conn.transaction():
                conn.execute("SET LOCAL lock_timeout='5s'")
                conn.execute("SET LOCAL statement_timeout='300s'")
                result=review_current(conn,as_of=dt.date.fromisoformat(args.as_of) if args.as_of else None,
                                     apply=args.apply,max_rows=args.max_rows,audit=args.output)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtextextended('finance-pipeline:knowledge_graph',0))")
    (args.output/'committed.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    return 0

if __name__=='__main__':raise SystemExit(main())
