import copy
import datetime as dt
import json
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
import graph_source_review as review


def record():
    now=dt.datetime(2026,10,14,tzinfo=dt.timezone.utc)
    return {'relation_version_id':'v','source_system':'research.reports','extractor_version':'temporal_research:v1',
      'status':'active','review_status':'unreviewed','relation_type':'RATES','document_id':'d',
      'attributes':{'document_id':'d','institution':'研究机构','stock_code':'300308'},
      'title':'研究材料','document_type':'research_report','publisher':'eastmoney',
      'public_at':now-dt.timedelta(days=2),'published_at':now-dt.timedelta(days=2),'system_at':now-dt.timedelta(days=1),
      'source_record_matches':True,'text_matches_document':True,'evidence_text':'可追溯的原文，研报给出的评级不等于公司确认事实。'}


class SourceReviewTests(unittest.TestCase):
    def test_classification_preserves_claim_not_fact(self):
        result=review.classify(record(),dt.datetime.now(dt.timezone.utc))
        self.assertTrue(result['eligible']);self.assertEqual(result['claim_class'],'extracted_claim')
        self.assertIn('not verified',result['meaning'])

    def test_missing_rejected_future_and_mismatched_sources_stay_excluded(self):
        now=dt.datetime.now(dt.timezone.utc)
        changes=[{'status':'candidate'},{'status':'rejected'},{'document_id':None},
                 {'source_readable':False},{'text_matches_document':False},{'source_record_matches':False},{'publisher':None},
                 {'public_at':now+dt.timedelta(days=1)}, {'system_at':now+dt.timedelta(days=1)},
                 {'source_system':'analytics.document_events'}]
        for change in changes:
            r=record();r.update(change)
            self.assertFalse(review.classify(r,now)['eligible'],change)
        for change in [{'supported':False},None]:
            r=record();r['source_system']='kg.company_concept_exposures';r['relation_type']='EXPOSED_TO'
            r['attributes']['attribution_check']=change
            self.assertFalse(review.classify(r,now)['eligible'])

    def test_reviewer_metadata_does_not_change_source_fingerprint(self):
        r=record();now=dt.datetime.now(dt.timezone.utc);first=review.classify(r,now)
        r['review_status']='classified';r['attributes']['evidence_review']=first
        self.assertEqual(first,review.classify(r,now))
        r['evidence_text']='原文改变'
        self.assertNotEqual(first['source_fingerprint'],review.classify(r,now)['source_fingerprint'])


def local_sql(sql, schema):
    return sql.replace('kg.',schema+'.').replace("'"+schema+".company_concept_exposures'", "'kg.company_concept_exposures'")

class CursorProxy:
    def __init__(self,cur,schema):self.cur,self.schema=cur,schema
    def __enter__(self):return self
    def __exit__(self,*args):self.cur.close()
    def executemany(self,sql,params):return self.cur.executemany(local_sql(sql,self.schema),params)
class ConnProxy:
    def __init__(self,conn,schema):self.conn,self.schema=conn,schema
    def execute(self,sql,params=None):return self.conn.execute(local_sql(sql,self.schema),params)
    def cursor(self):return CursorProxy(self.conn.cursor(),self.schema)


@unittest.skipUnless(os.getenv('FINAGENT_TEST_DB')=='1','rollback-only database test is opt-in')
class SourceReviewIntegrationTests(unittest.TestCase):
    def test_classification_bridge_idempotence_and_historical_replay(self):
        from common import connect
        root=Path(__file__).resolve().parents[1];schema='kg_review_test_'+uuid.uuid4().hex[:10]
        with connect(admin=True,autocommit=False,attempts=1) as conn:
            try:
                conn.execute("SET LOCAL statement_timeout='30s'")
                conn.execute('CREATE SCHEMA '+schema)
                for name in ['relations','relation_versions','relation_version_evidence','entity_versions','company_concept_exposures','market_entities','concept_edges']:
                    conn.execute(f'CREATE TABLE {schema}.{name} (LIKE kg.{name} INCLUDING ALL)')
                migration=(root/'docker/postgres/migrate_graph_revision_history_v5.sql').read_text().replace('BEGIN;','').replace('COMMIT;','').replace('kg.',schema+'.').replace('pg_catalog,kg','pg_catalog,'+schema)
                conn.execute(migration)
                conn.execute((root/'docker/postgres/migrate_graph_source_review_v6.sql').read_text().replace('kg.',schema+'.'))
                seed=conn.execute("""SELECT v.relation_version_id,v.relation_id,(v.attributes->>'exposure_id')::uuid exposure_id
                  FROM kg.relation_versions v JOIN kg.company_concept_exposures x ON x.exposure_id::text=v.attributes->>'exposure_id'
                  WHERE v.source_system='kg.company_concept_exposures' AND v.status='active' AND x.stock_code='300308'
                   AND v.valid_during @> (now() at time zone 'Asia/Shanghai')::date
                   AND x.metadata->'attribution_check'->>'supported'='true'
                   AND NOT EXISTS(SELECT 1 FROM corpus.document_quarantine q WHERE q.document_id=x.evidence_document_id AND q.released_at IS NULL) LIMIT 1""").fetchone()
                key=seed['relation_version_id']
                conn.execute(f'INSERT INTO {schema}.market_entities SELECT e.* FROM kg.market_entities e JOIN kg.relations r ON e.entity_id IN (r.source_entity_id,r.target_entity_id) WHERE r.relation_id=%s',(seed['relation_id'],))
                conn.execute(f'INSERT INTO {schema}.relations SELECT * FROM kg.relations WHERE relation_id=%s',(seed['relation_id'],))
                conn.execute(f'INSERT INTO {schema}.relation_versions SELECT * FROM kg.relation_versions WHERE relation_version_id=%s',(key,))
                conn.execute(f"UPDATE {schema}.relation_versions SET review_status='unreviewed',attributes=attributes-'evidence_review' WHERE relation_version_id=%s",(key,))
                conn.execute(f'INSERT INTO {schema}.company_concept_exposures SELECT * FROM kg.company_concept_exposures WHERE exposure_id=%s',(seed['exposure_id'],))
                before=conn.execute('SELECT clock_timestamp() t').fetchone()['t']
                proxy=ConnProxy(conn,schema)
                first=review.review_current(proxy,apply=True)
                self.assertEqual(first['evidence_links_written'],1)
                self.assertEqual(first['classification_changes'],1)
                self.assertEqual(conn.execute(f'SELECT review_status FROM {schema}.relation_versions').fetchone()['review_status'],'classified')
                past=conn.execute(f"SELECT review_status FROM {schema}.relation_versions_known_at(%s,'system')",(before,)).fetchone()
                self.assertEqual(past['review_status'],'unreviewed')
                # Scheduled supply rebuild must retain the reviewed state and
                # must not churn two revisions by removing/readding metadata.
                import build_temporal_knowledge_graph as builder
                from graph_revision_policy import RECONCILE_EXPOSURES_SQL
                history_before=conn.execute(f'SELECT count(*) n FROM {schema}.version_revision_history').fetchone()['n']
                for statement in builder.SUPPLY_SQL.split(';\n'):
                    if statement.strip():proxy.execute(statement,{'since':dt.date(2000,1,1),'until':dt.date.today()})
                proxy.execute(RECONCILE_EXPOSURES_SQL)
                second=review.review_current(proxy,apply=True)
                self.assertEqual(conn.execute(f'SELECT count(*) n FROM {schema}.version_revision_history').fetchone()['n'],history_before)
                self.assertEqual(second['classification_changes'],0);self.assertEqual(second['evidence_links_written'],0)
                # Changing source attribution revokes automatic classification.
                conn.execute(f"UPDATE {schema}.company_concept_exposures SET metadata=jsonb_set(metadata,'{{attribution_check,supported}}','false')")
                third=review.review_current(proxy,apply=True)
                self.assertEqual(third['classification_changes'],1)
                self.assertEqual(conn.execute(f'SELECT review_status FROM {schema}.relation_versions').fetchone()['review_status'],'unreviewed')
                conn.execute(f"UPDATE {schema}.relation_versions SET review_status='disputed'")
                self.assertEqual(review.review_current(proxy,apply=True)['rows_checked'],0)
            finally:conn.rollback()
