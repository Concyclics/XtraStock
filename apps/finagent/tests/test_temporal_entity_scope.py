import os,sys,unittest,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect
from build_temporal_knowledge_graph import ENTITY_VERSION_SQL
@unittest.skipUnless(os.getenv('FINAGENT_DB_TESTS')=='1','rollback-only DB test')
class EntityScopeTests(unittest.TestCase):
 def test_daily_scope_and_explicit_historical_range(self):
  q=(ENTITY_VERSION_SQL.split('), inferred AS')[0]+') SELECT canonical_key FROM scoped_entities ORDER BY canonical_key').replace('kg.market_entities','test_scope_entities').replace('analytics.document_events','test_scope_events')
  with connect(autocommit=False) as c:
   try:
    c.execute('CREATE TEMP TABLE test_scope_entities(canonical_key text,entity_type text);CREATE TEMP TABLE test_scope_events(id uuid,event_date date)')
    c.execute("INSERT INTO test_scope_entities VALUES('asset:company','company')")
    keys=[]
    for date in ['2020-01-01','2026-10-14','2026-10-16']:
     ident=uuid.uuid4();keys.append('event:'+str(ident));c.execute('INSERT INTO test_scope_events VALUES(%s,%s)',(ident,date));c.execute("INSERT INTO test_scope_entities VALUES(%s,'event')",(keys[-1],))
    daily=c.execute(q,{'since':'2026-10-11','until':'2026-10-14'}).fetchall()
    self.assertEqual({r['canonical_key'] for r in daily},{'asset:company',keys[1]})
    repair=c.execute(q,{'since':'2020-01-01','until':'2026-10-14'}).fetchall()
    self.assertEqual({r['canonical_key'] for r in repair},{'asset:company',keys[0],keys[1]})
   finally:c.rollback()
if __name__=='__main__':unittest.main()
