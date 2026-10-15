"""Real PostgreSQL semantics with temporary tables, always rolled back."""
import os,sys,unittest,uuid
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'scripts/analytics')]
from common import connect
from queue_temporal_llm_candidates import SQL
@unittest.skipUnless(os.getenv('FINAGENT_DB_TESTS')=='1','rollback-only DB tests')
class CandidateTests(unittest.TestCase):
 def test_scope_replay_and_changed_metadata(self):
  with connect(autocommit=False) as c:
   try:
    c.execute("""CREATE TEMP TABLE test_candidates(LIKE kg.extraction_candidates INCLUDING DEFAULTS INCLUDING INDEXES);
     CREATE TEMP TABLE test_docs(id uuid PRIMARY KEY,source_type text,source_name text,title text,published_at timestamptz,created_at timestamptz,content_text text,content_md text,raw_text text);
     CREATE TEMP TABLE test_reports(document_id uuid,report_date date,report_type text,stock_code text,industry_name text);""")
    ids=[uuid.uuid4() for _ in range(3)]
    for ident,date in zip(ids,['2026-10-14','2026-10-10','2026-10-15']):
     c.execute("INSERT INTO test_docs VALUES(%s,'research_report','eastmoney','AI订单',%s,now(),%s,NULL,NULL)",(ident,date,'AI服务器供应商预计中标订单。'*20))
     c.execute("INSERT INTO test_reports VALUES(%s,%s,'stock','600000',NULL)",(ident,date))
    sql=SQL.replace('kg.extraction_candidates','test_candidates').replace('corpus.documents','test_docs').replace('research.reports_resolved','test_reports').replace('research.reports','test_reports')
    params={'since':'2026-10-11','until':'2026-10-14','limit':20}
    self.assertEqual(c.execute(sql,params).rowcount,3)
    self.assertEqual(c.execute(sql,params).rowcount,0)
    self.assertEqual(c.execute('SELECT DISTINCT document_id FROM test_candidates').fetchall(),[{'document_id':ids[0]}])
    c.execute("UPDATE test_candidates SET status='ok'")
    c.execute("UPDATE test_docs SET title='AI新订单' WHERE id=%s",(ids[0],))
    self.assertEqual(c.execute(sql,params).rowcount,3)
    self.assertEqual(c.execute("SELECT count(*) n FROM test_candidates WHERE status='ok'").fetchone()['n'],3)
   finally:c.rollback()
if __name__=='__main__':unittest.main()
