import ast,os,sys,unittest,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect
@unittest.skipUnless(os.getenv('FINAGENT_DB_TESTS')=='1','rollback-only DB test')
class EntityRefreshTests(unittest.TestCase):
 def test_event_entity_noop_and_changed_source(self):
  tree=ast.parse(Path('scripts/analytics/build_market_knowledge_graph.py').read_text())
  sql=next(n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'INSERT INTO kg.market_entities' in n.value and "SELECT 'event'" in n.value)
  sql=sql.replace('kg.market_entities','test_graph_entities').replace('analytics.document_events','test_graph_events')
  with connect(autocommit=False) as c:
   try:
    c.execute('CREATE TEMP TABLE test_graph_entities(LIKE kg.market_entities INCLUDING DEFAULTS INCLUDING INDEXES)')
    c.execute('CREATE TEMP TABLE test_graph_events(id uuid,event_date date,summary text,event_type text,polarity int,impact_score float)')
    c.execute("INSERT INTO test_graph_events VALUES(%s,'2026-10-14','公司签署订单','order',1,.8)",(uuid.uuid4(),))
    params={'since':'2026-10-11','as_of':'2026-10-14'}
    self.assertEqual(c.execute(sql,params).rowcount,1)
    before=c.execute('SELECT * FROM test_graph_entities').fetchone()
    self.assertEqual(c.execute(sql,params).rowcount,0)
    self.assertEqual(c.execute('SELECT * FROM test_graph_entities').fetchone(),before)
    c.execute("UPDATE test_graph_events SET summary='公司更正订单金额',impact_score=.5")
    self.assertEqual(c.execute(sql,params).rowcount,1)
    after=c.execute('SELECT * FROM test_graph_entities').fetchone()
    self.assertEqual(after['name'],'公司更正订单金额');self.assertEqual(after['metadata']['impact_score'],.5)
   finally:c.rollback()
if __name__=='__main__':unittest.main()
