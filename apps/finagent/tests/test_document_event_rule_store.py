"""Persistence regressions against temporary PostgreSQL tables; all changes rolled back."""
import datetime as dt
import os
import sys
import unittest
import uuid
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts/analytics'),str(ROOT)]
from common import connect, jsonb
from document_event_rule_store import (persist_document,input_signature,RetryDocument,
    GRAPH_WRITE_LOCK,CANDIDATE_SQL,commit_batch)
from extract_document_events_rules import CompanyResolver

# Temporary-table tests must never contend with the production graph writer.
GRAPH_WRITE_LOCK = 1704201313 + os.getpid()

TABLES = {
 'corpus.documents':'test_rule_documents','research.reports_resolved':'test_rule_reports','research.reports':'test_rule_reports',
 'corpus.document_quarantine':'test_rule_quarantine','market.assets':'test_rule_assets',
 'analytics.document_events':'test_rule_events','corpus.document_keywords':'test_rule_keywords',
 'kg.document_extraction_status':'test_rule_status','analytics.document_rule_revisions':'test_rule_revisions',
 'kg.relation_versions':'test_rule_graph',
}

class MappedCursor:
 def __init__(self,cur):self.cur=cur
 def execute(self,sql,params=None):
  for key,value in TABLES.items():sql=sql.replace(key,value)
  sql=sql.replace("'test_rule_events'","'analytics.document_events'")
  self.cur.execute(sql,params);return self
 def __getattr__(self,key):return getattr(self.cur,key)
 def __enter__(self):return self
 def __exit__(self,*args):self.cur.close()

class MappedConnection:
 def __init__(self,conn):self.conn=conn
 def cursor(self):return MappedCursor(self.conn.cursor())
 def __getattr__(self,key):return getattr(self.conn,key)

@unittest.skipUnless(os.environ.get('FINAGENT_DB_TESTS')=='1','explicit rollback-only SQL tests')
class RuleStoreTests(unittest.TestCase):
 def setUp(self):
  lock_patch=patch('document_event_rule_store.GRAPH_WRITE_LOCK',GRAPH_WRITE_LOCK)
  lock_patch.start();self.addCleanup(lock_patch.stop)
  self.c=connect(autocommit=False);self.c.execute("SET LOCAL statement_timeout='25s'")
  self.c.execute("""CREATE TEMP TABLE test_rule_documents(id uuid PRIMARY KEY,updated_at timestamptz);
   CREATE TEMP TABLE test_rule_reports(document_id uuid,updated_at timestamptz,identity_known_at timestamptz);
   CREATE TEMP TABLE test_rule_quarantine(document_id uuid,released_at timestamptz);
   CREATE TEMP TABLE test_rule_assets(asset_id uuid,asset_type text,symbol text,metadata jsonb);
   CREATE TEMP TABLE test_rule_events(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),document_id uuid,
    event_date date,event_type text,polarity smallint,impact_score double precision,confidence double precision,
    entity_texts text[],asset_ids uuid[],stock_codes text[],industries text[],keywords text[],summary text,evidence text,
    extraction_method text,event_hash text UNIQUE,metadata jsonb,created_at timestamptz DEFAULT now(),updated_at timestamptz);
   CREATE TEMP TABLE test_rule_keywords(id bigserial PRIMARY KEY,document_id uuid,keyword text,weight numeric,method text,
    UNIQUE(document_id,keyword,method));
   CREATE TEMP TABLE test_rule_status(document_id uuid,extraction_method text,status text,event_count int,keyword_count int,
    processed_at timestamptz,source_updated_at timestamptz,details jsonb,PRIMARY KEY(document_id,extraction_method));
   CREATE TEMP TABLE test_rule_revisions(revision_id uuid DEFAULT gen_random_uuid(),document_id uuid,
    extraction_method text,object_type text,source_object_id text,reason text,input_signature text,previous_row jsonb,
    recorded_at timestamptz DEFAULT now());
   CREATE TEMP TABLE test_rule_graph(relation_version_id uuid DEFAULT gen_random_uuid(),extractor_version text,
    source_system text,status text,attributes jsonb,updated_at timestamptz);
  """)
  self.doc=str(uuid.uuid4());self.asset=str(uuid.uuid4());self.at=dt.datetime(2026,10,13,12,tzinfo=dt.timezone.utc)
  self.c.execute('INSERT INTO test_rule_documents VALUES(%s,%s)',(self.doc,self.at))
  self.c.execute("INSERT INTO test_rule_assets VALUES(%s,'stock','600491','{}')",(self.asset,))
  self.row={'document_id':self.doc,'source_updated_at':self.at,'document_updated_at':self.at,'report_updated_at':None}
  self.event=dict(document_id=self.doc,event_date=dt.date(2026,10,13),event_type='order_contract',polarity=1,
   impact_score=.7,confidence=.8,entity_texts=['600491'],stock_codes=['600491'],industries=[],keywords=['中标'],
   summary='中标',evidence='公司中标',extraction_method='rules:v2',event_hash='stablehash',metadata={})
  self.cur=MappedCursor(self.c.cursor())
 def tearDown(self):self.c.rollback();self.c.close()
 def apply(self,events=None,keywords=None,signature='sig',row=None,force=False):
  return persist_document(self.cur,row or self.row,[self.event] if events is None else events,
   [('中标',.7)] if keywords is None else keywords,'rules:v2','entity_clause:v5',signature,'catalog',force=force)
 def test_insert_populates_asset_ids_without_aliases(self):
  self.c.execute("INSERT INTO test_rule_assets VALUES(%s,'stock','600491','{\"identity_alias_of\":\"x\"}')",(uuid.uuid4(),))
  self.apply();r=self.c.execute('SELECT asset_ids FROM test_rule_events').fetchone();self.assertEqual(list(map(str,r['asset_ids'])),[self.asset])
 def test_identity_resolution_change_invalidates_stale_bounded_read(self):
  known=self.at+dt.timedelta(minutes=1)
  self.c.execute('INSERT INTO test_rule_reports(document_id,updated_at,identity_known_at) VALUES(%s,%s,%s)',(self.doc,self.at,known))
  stale={**self.row,'report_updated_at':self.at}
  with self.assertRaises(RetryDocument):self.apply(row=stale)
  self.assertEqual(self.c.execute('SELECT count(*) n FROM test_rule_events').fetchone()['n'],0)
  current={**stale,'report_updated_at':known,'source_updated_at':known}
  self.assertEqual(self.apply(row=current)['documents'],1)
 def test_exact_forced_replay_writes_nothing(self):
  self.apply();old=self.c.execute('SELECT * FROM test_rule_status').fetchone();r=self.apply(force=True)
  self.assertEqual(r['unchanged_inputs'],1)
  for key in ('events_written','keywords_written','status_written','event_revisions'):self.assertEqual(r[key],0,key)
  self.assertEqual(old,self.c.execute('SELECT * FROM test_rule_status').fetchone())
 def test_force_reconciles_modified_output_without_touching_input(self):
  self.apply();self.c.execute("UPDATE test_rule_events SET polarity=-1")
  r=self.apply(force=True);self.assertEqual(r['events_written'],1);self.assertEqual(r['event_revisions'],1)
  self.assertEqual(self.c.execute('SELECT polarity FROM test_rule_events').fetchone()['polarity'],1)
 def test_metadata_only_version_bump_advances_watermark_not_outputs(self):
  self.apply();later=self.at+dt.timedelta(minutes=1);self.c.execute('UPDATE test_rule_documents SET updated_at=%s',(later,))
  row={**self.row,'source_updated_at':later,'document_updated_at':later};r=self.apply(row=row)
  self.assertEqual(r['unchanged_inputs'],1);self.assertEqual(r['status_written'],1);self.assertEqual(r['events_written'],0)
 def test_changed_body_same_semantics_only_updates_status(self):
  self.apply();r=self.apply(signature='bodychanged');self.assertEqual(r['events_written'],0);self.assertEqual(r['keywords_written'],0);self.assertEqual(r['status_written'],1)
 def test_zero_outputs_archive_and_invalidate_precise_method(self):
  self.apply();eventid=self.c.execute('SELECT id FROM test_rule_events').fetchone()['id']
  self.c.execute("INSERT INTO test_rule_graph(extractor_version,source_system,status,attributes) VALUES('temporal_events:v1','analytics.document_events','active',%s)",(jsonb({'event_id':str(eventid)}),))
  self.c.execute("INSERT INTO test_rule_events(document_id,extraction_method,event_hash) VALUES(%s,'llm:v1','llmhash')",(self.doc,))
  self.c.execute("INSERT INTO test_rule_keywords(document_id,keyword,weight,method) VALUES(%s,'保留',1,'llm:v1')",(self.doc,))
  r=self.apply(events=[],keywords=[],signature='emptybody');self.assertEqual(r['events_deleted'],1);self.assertEqual(r['keywords_deleted'],1);self.assertEqual(r['graph_versions_archived'],1)
  self.assertEqual(self.c.execute('SELECT status FROM test_rule_graph').fetchone()['status'],'archived')
  self.assertEqual(self.c.execute('SELECT extraction_method FROM test_rule_events').fetchone()['extraction_method'],'llm:v1')
  self.assertEqual(self.c.execute('SELECT count(*) n FROM test_rule_revisions').fetchone()['n'],2)
  self.assertEqual(self.c.execute('SELECT event_count FROM test_rule_status').fetchone()['event_count'],0)
 def test_same_hash_changed_date_or_polarity_is_versioned(self):
  self.apply();event={**self.event,'event_date':dt.date(2026,10,12),'polarity':-1}
  r=self.apply(events=[event],signature='changed');self.assertEqual(r['event_revisions'],1)
  self.assertEqual(self.c.execute('SELECT polarity FROM test_rule_events').fetchone()['polarity'],-1)
  self.assertEqual(self.c.execute("SELECT previous_row->>'polarity' p FROM test_rule_revisions WHERE object_type='event'").fetchone()['p'],'1')
 def test_source_change_never_marks_newer_version_done(self):
  later=self.at+dt.timedelta(seconds=1);self.c.execute('UPDATE test_rule_documents SET updated_at=%s',(later,))
  with self.assertRaisesRegex(RetryDocument,'source changed'):self.apply()
  self.assertEqual(self.c.execute('SELECT count(*) n FROM test_rule_status').fetchone()['n'],0)
 def test_report_change_is_part_of_read_version(self):
  self.c.execute('INSERT INTO test_rule_reports(document_id,updated_at) VALUES(%s,%s)',(self.doc,self.at))
  with self.assertRaisesRegex(RetryDocument,'source changed'):self.apply()
 def test_busy_graph_lock_leaves_old_outputs(self):
  self.apply()
  with connect() as other:
   if not other.execute('SELECT pg_try_advisory_lock(%s) ok',(GRAPH_WRITE_LOCK,)).fetchone()['ok']:
    self.skipTest('live graph writer owns advisory lock')
   try:
    with self.assertRaisesRegex(RetryDocument,'graph revision writer busy'):self.apply(events=[],keywords=[],signature='new')
    self.assertEqual(self.c.execute('SELECT count(*) n FROM test_rule_events').fetchone()['n'],1)
    self.assertEqual(self.c.execute('SELECT count(*) n FROM test_rule_revisions').fetchone()['n'],0)
   finally:other.execute('SELECT pg_advisory_unlock(%s)',(GRAPH_WRITE_LOCK,))
 def test_short_body_explicit_skip_invalidates_old_output(self):
  self.apply();self.apply(events=[],keywords=[],signature='short',row={**self.row,'_skip_reason':'announcement_body_too_short'})
  r=self.c.execute('SELECT * FROM test_rule_status').fetchone();self.assertEqual(r['status'],'skipped');self.assertEqual(r['details']['skip_reason'],'announcement_body_too_short')
 def test_batch_contention_rolls_back_previous_document(self):
  second='ffffffff-ffff-ffff-ffff-ffffffffffff'
  self.c.execute('INSERT INTO test_rule_documents VALUES(%s,%s)',(second,self.at+dt.timedelta(seconds=1)))
  self.c.commit()  # Only temporary tables exist on this test connection.
  bad={**self.row,'document_id':second}
  args=SimpleNamespace(method='rules:v2',logic_version='entity_clause:v5',force=False)
  with self.assertRaisesRegex(RetryDocument,'source changed'):
   commit_batch(MappedConnection(self.c),[(self.row,[self.event],[('中标',.7)],'sig'),(bad,[],[],'other')],args,'catalog')
  self.assertEqual(self.c.execute('SELECT count(*) n FROM test_rule_events').fetchone()['n'],0)
  self.assertEqual(self.c.execute('SELECT count(*) n FROM test_rule_status').fetchone()['n'],0)
 def test_weight_change_preserves_before_image(self):
  self.apply();r=self.apply(keywords=[('中标',.8)],signature='weights');self.assertEqual(r['keyword_revisions'],1);self.assertEqual(r['keywords_written'],1)
  self.assertEqual(float(self.c.execute("SELECT (previous_row->>'weight')::numeric w FROM test_rule_revisions WHERE object_type='keyword'").fetchone()['w']),.7)

 def test_candidate_dates_versions_quarantine_and_short_body_cleanup(self):
  self.c.execute("""ALTER TABLE test_rule_documents ADD source_type text, ADD published_at timestamptz,
    ADD content_text text, ADD content_md text, ADD raw_text text;
    ALTER TABLE test_rule_reports ADD report_date date;""")
  ids={name:str(uuid.uuid4()) for name in ['recent','old','updated_old','future','short','short_cleanup','quarantined','processed','report']}
  for name,doc in ids.items():
   date='2026-10-12' if name not in ('old','updated_old','future') else ('2026-10-16' if name=='future' else '2025-01-01')
   updated=dt.datetime.now(dt.timezone.utc) if name=='updated_old' else (dt.datetime(2025,1,1,tzinfo=dt.timezone.utc) if name=='old' else self.at)
   self.c.execute("""INSERT INTO test_rule_documents(id,updated_at,source_type,published_at,content_text)
       VALUES(%s,%s,%s,%s,%s)""",(doc,updated,'research_report' if name=='report' else 'announcement',date,
       'short' if name in ('short','short_cleanup') else '正文'*150))
  self.c.execute('INSERT INTO test_rule_quarantine VALUES(%s,NULL)',(ids['quarantined'],))
  self.c.execute("INSERT INTO test_rule_keywords(document_id,keyword,weight,method) VALUES(%s,'旧内容',1,'rules:v2')",(ids['short_cleanup'],))
  self.c.execute("""INSERT INTO test_rule_status(document_id,extraction_method,status,source_updated_at,details)
    VALUES(%s,'rules:v2','ok',%s,'{"logic_version":"entity_clause:v5"}')""",(ids['processed'],self.at))
  self.c.execute('INSERT INTO test_rule_reports(document_id,updated_at,report_date) VALUES(%s,%s,%s)',(ids['report'],self.at,'2026-10-16'))
  params=dict(source_types=None,document_ids=None,since='2026-10-11',until='2026-10-14',updated_hours=72,
    force=False,method='rules:v2',logic_version='entity_clause:v5',limit=100)
  actual={r['document_id'] for r in self.cur.execute(CANDIDATE_SQL,params).fetchall()}
  self.assertEqual(actual,{ids[k] for k in ('recent','updated_old','short','short_cleanup')})

class FingerprintTests(unittest.TestCase):
 def test_ingestion_timestamp_is_not_content(self):
  a={'document_id':'x','content_text':'原文','source_updated_at':'yesterday'};b={**a,'source_updated_at':'today'}
  self.assertEqual(input_signature(a,'v2','v5','rules'),input_signature(b,'v2','v5','rules'))
  self.assertNotEqual(input_signature(a,'v2','v5','rules'),input_signature({**b,'content_text':'改文'},'v2','v5','rules'))
 def test_issuer_mapping_precomputed(self):
  self.assertEqual(CompanyResolver([{'symbol':'600491','name':'龙元建设'}]).names_by_code,{'600491':'龙元建设'})
 def test_candidates_include_report_version_and_bounded_limit(self):
    self.assertIn('greatest(d.updated_at,r.updated_at,r.identity_known_at)',CANDIDATE_SQL);self.assertIn('LIMIT %(limit)s',CANDIDATE_SQL)

if __name__=='__main__':unittest.main()
