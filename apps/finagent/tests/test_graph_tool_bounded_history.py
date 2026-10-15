import ast,datetime as dt,os,sys,unittest,uuid
from pathlib import Path
from psycopg.types.json import Jsonb
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect
@unittest.skipUnless(os.getenv('FINAGENT_DB_TESTS')=='1','rollback-only DB test')
class BoundedHistoryTests(unittest.TestCase):
 def test_current_and_frozen_versions_stay_disjoint(self):
  tree=ast.parse(Path('MMClaw/mmclaw/finance_db_helper.py').read_text())
  fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='run_v3_graph_analysis')
  sql=next(n.value for n in ast.walk(fn) if isinstance(n,ast.Constant) and isinstance(n.value,str) and 'WITH matched_entities' in n.value)
  sql=sql.split('\n            SELECT rv.relation_version_id')[0]+' SELECT relation_version_id,confidence FROM matching_versions'
  tables=['market_entities','relations','relation_versions','version_revision_history']
  for name in tables:sql=sql.replace('kg.'+name,'test_bounded_'+name)
  with connect(autocommit=False) as c:
   try:
    for name in tables:c.execute(f'CREATE TEMP TABLE test_bounded_{name}(LIKE kg.{name} INCLUDING DEFAULTS INCLUDING INDEXES)')
    source,target,relation,version=[uuid.uuid4() for _ in range(4)]
    c.execute("INSERT INTO test_bounded_market_entities(entity_id,entity_type,canonical_key,name,normalized_name,metadata) VALUES(%s,'company','test:company','测试公司','测试公司','{\"stock_code\":\"600000\"}'),(%s,'product','test:product','测试产品','测试产品','{}')",(source,target))
    c.execute("INSERT INTO test_bounded_relations(relation_id,relation_key,source_entity_id,target_entity_id,relation_type) VALUES(%s,'test:relation',%s,%s,'PRODUCES')",(relation,source,target))
    c.execute("""INSERT INTO test_bounded_relation_versions(relation_version_id,relation_id,version_key,valid_during,public_known_during,system_known_during,confidence,status,review_status,source_system,extractor_version)
    VALUES(%s,%s,'test:v','[2026-01-01,)','[2026-01-01,)','[2026-10-14,)',.9,'active','approved','test','test')""",(version,relation))
    c.execute("""INSERT INTO test_bounded_version_revision_history(version_kind,source_version_id,system_known_during,frozen_record,frozen_evidence,closed_at)
      SELECT 'relation',relation_version_id,'[2026-10-13,2026-10-14)',to_jsonb(v)||'{"confidence":0.4,"system_known_during":"[2026-10-13,2026-10-14)"}'::jsonb,'[]','2026-10-14' FROM test_bounded_relation_versions v""")
    for at,expected in [('2026-10-12T12:00:00Z',[]),('2026-10-13T12:00:00Z',[.4]),('2026-10-14T00:00:00Z',[.9])]:
     rows=c.execute(sql,{'cutoff':at,'query':'','has_codes':True,'codes':['600000']}).fetchall()
     self.assertEqual([r['confidence'] for r in rows],expected)
   finally:c.rollback()
if __name__=='__main__':unittest.main()
