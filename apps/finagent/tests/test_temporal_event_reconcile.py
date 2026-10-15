"""Rollback-only tests for selective event reconciliation and corrected direction."""
import os,sys,unittest,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect
from build_temporal_knowledge_graph import EVENT_SQL,EVENT_RECONCILE_SQL,execute_batch
MAP={'kg.market_entities':'test_event_entities','kg.relations':'test_event_relations','kg.relation_versions':'test_event_versions','kg.relation_version_evidence':'test_event_evidence','market.assets':'test_event_assets','analytics.document_events':'test_source_events','corpus.documents':'test_event_docs'}
def mapped(sql):
 for name,target in MAP.items():sql=sql.replace(name,target)
 return sql
@unittest.skipUnless(os.getenv('FINAGENT_DB_TESTS')=='1','rollback-only DB test')
class EventTests(unittest.TestCase):
 def test_replay_removed_associations_and_changed_event(self):
  with connect(autocommit=False) as c:
   try:
    c.execute("SET LOCAL statement_timeout='25s'")
    for source in ['kg.market_entities','kg.relations','kg.relation_versions','kg.relation_version_evidence']:
     c.execute(f'CREATE TEMP TABLE {MAP[source]} (LIKE {source} INCLUDING DEFAULTS INCLUDING INDEXES)')
    c.execute('''CREATE TEMP TABLE test_event_assets(asset_id uuid,asset_type text,symbol text);
      CREATE TEMP TABLE test_event_docs(id uuid,published_at timestamptz,created_at timestamptz);
      CREATE TEMP TABLE test_source_events(id uuid,document_id uuid,event_date date,event_type text,polarity int,impact_score float,confidence float,summary text,evidence text,extraction_method text,created_at timestamptz,updated_at timestamptz,asset_ids uuid[],stock_codes text[],industries text[]);''')
    asset,doc,event=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
    c.execute("INSERT INTO test_event_assets VALUES(%s,'stock','600000')",(asset,))
    c.execute("INSERT INTO test_event_docs VALUES(%s,'2026-10-13T09:00:00+08','2026-10-13T09:00:00+08')",(doc,))
    c.execute("INSERT INTO test_event_entities(entity_type,canonical_key,name,normalized_name) VALUES('company',%s,'测试公司','测试公司'),('industry','industry:'||md5('半导体'),'半导体','半导体')",('asset:'+str(asset),))
    c.execute("INSERT INTO test_source_events VALUES(%s,%s,'2026-10-13','order',1,.8,.9,'订单增加','公司签署订单','rules:v2',now(),now(),ARRAY[]::uuid[],ARRAY['600000'],ARRAY['半导体'])",(event,doc))
    params={'since':'2026-10-11','until':'2026-10-14'}
    with c.cursor() as cur:execute_batch(cur,mapped(EVENT_SQL),params)
    self.assertEqual(c.execute("SELECT count(*) n FROM test_event_versions WHERE status='active'").fetchone()['n'],2)
    # Existing pairs are not archived just to reactivate them immediately.
    self.assertEqual(c.execute(mapped(EVENT_RECONCILE_SQL),params).rowcount,0)
    c.execute("UPDATE test_source_events SET polarity=-1,event_date='2026-10-14',summary='订单减少'")
    with c.cursor() as cur:execute_batch(cur,mapped(EVENT_SQL),params)
    current=c.execute('SELECT direction,lower(valid_during) dt FROM test_event_versions').fetchall()
    self.assertTrue(all(r['direction']==-1 and str(r['dt'])=='2026-10-14' for r in current))
    # Remove the company attribution but retain industry attribution.
    c.execute("UPDATE test_source_events SET stock_codes=ARRAY[]::text[]")
    self.assertEqual(c.execute(mapped(EVENT_RECONCILE_SQL),params).rowcount,1)
    self.assertEqual(c.execute(mapped(EVENT_RECONCILE_SQL),params).rowcount,0)
    c.execute('DELETE FROM test_source_events')
    self.assertEqual(c.execute(mapped(EVENT_RECONCILE_SQL),params).rowcount,1)
    self.assertEqual(c.execute("SELECT count(*) n FROM test_event_versions WHERE status='active'").fetchone()['n'],0)
   finally:c.rollback()
if __name__=='__main__':unittest.main()
