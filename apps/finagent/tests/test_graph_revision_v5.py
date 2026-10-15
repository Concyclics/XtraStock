from __future__ import annotations
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/analytics'))
from common import connect
from graph_revision_policy import RECONCILE_EXPOSURES_SQL
path=Path(os.environ.get('GRAPH_FEATURE_MODULE_PATH',str(ROOT/'scripts/analytics/build_temporal_graph_features_daily.py')))
spec=importlib.util.spec_from_file_location('graph_features_test',path)
features=importlib.util.module_from_spec(spec);spec.loader.exec_module(features)

class CutoffTests(unittest.TestCase):
    def test_current_day_uses_actual_time_and_past_day_stops_at_day_end(self):
        now=dt.datetime(2026,10,13,14,0,tzinfo=dt.timezone.utc)
        self.assertEqual(features.resolve_decision_cutoff(dt.date(2026,10,13),now=now),now)
        self.assertEqual(features.resolve_decision_cutoff(dt.date(2026,10,12),now=now),dt.datetime(2026,10,12,15,59,59,tzinfo=dt.timezone.utc))
    def test_naive_and_future_cutoffs_rejected(self):
        now=dt.datetime(2026,10,13,14,tzinfo=dt.timezone.utc)
        for value in ['2026-10-13T12:00:00','2026-10-14T12:00:00+08:00']:
            with self.assertRaises(ValueError):features.resolve_decision_cutoff(now.date(),value,now=now)

@unittest.skipUnless(os.environ.get('FINAGENT_TEST_DB')=='1','transactional database integration test is opt-in')
class RevisionIntegrationTests(unittest.TestCase):
    def test_immutable_revisions_exact_system_replay_and_formal_candidate_exclusion(self):
        schema='kg_graph_revision_test_'+uuid.uuid4().hex[:10]
        with connect(admin=True,autocommit=False,attempts=1) as conn:
            try:
                conn.execute("SET LOCAL statement_timeout='30s'")
                conn.execute("SET LOCAL lock_timeout='3s'")
                conn.execute('CREATE SCHEMA '+schema)
                for name in ['relations','relation_versions','entity_versions','relation_version_evidence','market_entities','source_coverage_daily','company_concept_exposures']:
                    conn.execute(f'CREATE TABLE {schema}.{name} (LIKE kg.{name} INCLUDING ALL)')
                migration=(ROOT/'docker/postgres/migrate_graph_revision_history_v5.sql').read_text()
                migration=migration.replace('BEGIN;','').replace('COMMIT;','').replace('kg.',schema+'.').replace('pg_catalog,kg','pg_catalog,'+schema)
                conn.execute(migration)
                seed=conn.execute("SELECT v.relation_version_id,r.relation_id,r.source_entity_id,r.target_entity_id FROM kg.relation_versions v JOIN kg.relations r USING(relation_id) WHERE v.status='active' AND v.source_system='company_concept_exposures' AND r.relation_type='EXPOSED_TO' LIMIT 1").fetchone()
                conn.execute(f'INSERT INTO {schema}.relations SELECT * FROM kg.relations WHERE relation_id=%s',(seed['relation_id'],))
                conn.execute(f'INSERT INTO {schema}.market_entities SELECT * FROM kg.market_entities WHERE entity_id=ANY(%s)',([seed['source_entity_id'],seed['target_entity_id']],))
                conn.execute(f'INSERT INTO {schema}.relation_versions SELECT r.* FROM kg.relation_versions v CROSS JOIN LATERAL jsonb_populate_record(NULL::{schema}.relation_versions,to_jsonb(v)) r WHERE v.relation_version_id=%s',(seed['relation_version_id'],))
                key=seed['relation_version_id']
                original=conn.execute(f'SELECT confidence,lower(system_known_during) AS start,lower(valid_during) AS valid_at FROM {schema}.relation_versions WHERE relation_version_id=%s',(key,)).fetchone()
                conn.execute(f"INSERT INTO {schema}.relation_version_evidence(relation_version_id,system_at,evidence_hash,evidence_text) VALUES(%s,now(),'integration-source','frozen original evidence')",(key,))
                changed=0.234567 if original['confidence']!=0.234567 else 0.345678
                conn.execute(f'UPDATE {schema}.relation_versions SET confidence=%s WHERE relation_version_id=%s',(changed,key))
                historic=conn.execute(f'SELECT * FROM {schema}.version_revision_history').fetchone()
                self.assertEqual(float(historic['frozen_record']['confidence']),original['confidence'])
                self.assertEqual(historic['frozen_evidence'][0]['evidence_text'],'frozen original evidence')
                self.assertFalse(historic['system_known_during'].upper_inf)
                replay=conn.execute(f"SELECT confidence FROM {schema}.relation_versions_known_at(%s,'system') WHERE relation_version_id=%s",(original['start'],key)).fetchone()
                self.assertEqual(replay['confidence'],original['confidence'])
                conn.execute(f"UPDATE {schema}.relation_versions SET updated_at=now(),system_known_during=tstzrange('2020-01-01'::timestamptz,NULL,'[)') WHERE relation_version_id=%s",(key,))
                self.assertEqual(conn.execute(f'SELECT count(*) n FROM {schema}.version_revision_history').fetchone()['n'],1)
                conn.execute(f"UPDATE {schema}.relation_version_evidence SET evidence_text='corrected evidence' WHERE relation_version_id=%s",(key,))
                self.assertEqual(conn.execute(f'SELECT count(*) n FROM {schema}.version_revision_history').fetchone()['n'],2)
                evidence_history=conn.execute(f'SELECT frozen_evidence FROM {schema}.version_revision_history ORDER BY closed_at DESC LIMIT 1').fetchone()['frozen_evidence']
                self.assertEqual(evidence_history[0]['evidence_text'],'frozen original evidence')
                conn.execute(f"UPDATE {schema}.relation_versions SET status='candidate' WHERE relation_version_id=%s",(key,))
                now=conn.execute('SELECT clock_timestamp() t').fetchone()['t']
                formal=conn.execute(f"SELECT count(*) n FROM {schema}.temporal_edges_at(%s,%s,'system')",(original['valid_at'],now)).fetchone()['n']
                self.assertEqual(formal,0)
                with self.assertRaises(Exception):
                    with conn.transaction():conn.execute(f"UPDATE {schema}.version_revision_history SET frozen_record='{{}}'")
                # A legacy source row that was rejected must be rejected in every graph naming path.
                exposure=conn.execute("SELECT split_part(relation_key,':',3)::uuid id FROM kg.relations WHERE relation_id=%s",(seed['relation_id'],)).fetchone()['id']
                conn.execute(f'INSERT INTO {schema}.company_concept_exposures SELECT * FROM kg.company_concept_exposures WHERE exposure_id=%s',(exposure,))
                conn.execute(f"UPDATE {schema}.company_concept_exposures SET status='rejected' WHERE exposure_id=%s",(exposure,))
                conn.execute(RECONCILE_EXPOSURES_SQL.replace('kg.',schema+'.'))
                corrected=conn.execute(f'SELECT status,attributes FROM {schema}.relation_versions WHERE relation_version_id=%s',(key,)).fetchone()
                self.assertEqual(corrected['status'],'rejected')
                self.assertTrue(corrected['attributes']['superseded_by_canonical'])
                # Duplicate evidence versions for one company/concept must contribute only once.
                base=conn.execute(f'SELECT to_jsonb(v) body FROM {schema}.relation_versions v WHERE relation_version_id=%s',(key,)).fetchone()['body']
                for i,confidence in enumerate([0.7,0.9]):
                    record=dict(base);record.update(relation_version_id=str(uuid.uuid4()),version_key='dedup-test-'+str(i),
                        status='active',confidence=confidence,weight=1,directness='direct',source_system='kg.company_concept_exposures',
                        attributes={'attribution_check':{'supported':True}})
                    conn.execute(f'INSERT INTO {schema}.relation_versions SELECT r.* FROM jsonb_populate_record(NULL::{schema}.relation_versions,%s::jsonb) r',(json.dumps(record),))
                event_relation=uuid.uuid4();event_version=uuid.uuid4()
                conn.execute(f"INSERT INTO {schema}.relations(relation_id,relation_key,source_entity_id,target_entity_id,relation_type) VALUES(%s,'event-evidence-test',%s,%s,'AFFECTED_BY_EVENT')",(event_relation,seed['source_entity_id'],seed['target_entity_id']))
                event=dict(base);event.update(relation_version_id=str(event_version),relation_id=str(event_relation),version_key='event-evidence-test',
                    status='active',confidence=0.7,weight=2,direction=1,source_system='analytics.document_events',valid_during='[2026-10-12,2026-10-13)',
                    public_known_during='[2026-10-11 00:00:00+00,)',attributes={'event_type':'order_contract'})
                conn.execute(f'INSERT INTO {schema}.relation_versions SELECT r.* FROM jsonb_populate_record(NULL::{schema}.relation_versions,%s::jsonb) r',(json.dumps(event),))
                for i in range(2):
                    conn.execute(f"INSERT INTO {schema}.relation_version_evidence(relation_version_id,document_id,public_at,system_at,evidence_hash) VALUES(%s,%s,'2026-10-11',clock_timestamp(),%s)",(event_version,uuid.uuid4(),'multiple-evidence-'+str(i)))
                now=conn.execute('SELECT clock_timestamp() t').fetchone()['t']
                select_features=features.SQL.split('INSERT INTO analytics.graph_feature_daily')[0]+'SELECT asset_id,exposure_count,direct_exposure_score,positive_event_score_30d,evidence_document_count_30d FROM final'
                output=conn.execute(select_features.replace('kg.',schema+'.'),{'as_of':dt.date(2026,10,12),'decision_cutoff':now,'knowledge_mode':'system'}).fetchall()
                positives=[r for r in output if r['exposure_count']>0]
                self.assertEqual(len(positives),1)
                self.assertEqual(positives[0]['exposure_count'],1)
                self.assertAlmostEqual(positives[0]['direct_exposure_score'],0.9)
                self.assertAlmostEqual(positives[0]['positive_event_score_30d'],1.4)
                self.assertEqual(positives[0]['evidence_document_count_30d'],2)
                # Validate the complete production feature SQL against real table types without executing its INSERT.
                graph_sql=features.SQL.replace('kg.',schema+'.')
                conn.execute('EXPLAIN '+graph_sql,{'as_of':dt.date(2026,10,12),'decision_cutoff':now,'knowledge_mode':'system'}).fetchall()
                # The scratch schema is rolled back, including all functions, triggers and seed rows.
            finally:conn.rollback()

if __name__=='__main__':unittest.main()
