import os,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect
@unittest.skipUnless(os.getenv('FINAGENT_DB_TESTS')=='1','rollback-only DB test')
class ImportTests(unittest.TestCase):
 def test_empty_legacy_import_preserves_recovered_body_and_version(self):
  source=Path('docker/postgres/import_eastmoney_reports_materialize.sql').read_text()
  sql=source[source.index('INSERT INTO corpus.documents'):source.index('INSERT INTO research.reports')].replace('corpus.documents','test_import_documents')
  with connect(autocommit=False) as c:
   try:
    c.execute('CREATE TEMP TABLE test_import_documents(LIKE corpus.documents INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)')
    c.execute('CREATE TEMP TABLE eastmoney_reports_dedup(source_type text,source_name text,external_id text,title text,publisher text,published_at text,url text,language text,content_text text,content_md text,raw_text text,metadata jsonb,content_hash text)')
    c.execute("INSERT INTO eastmoney_reports_dedup(source_type,source_name,external_id,title,content_text,content_hash,metadata) VALUES('research_report','eastmoney','test-recovered','测试','完整正文','original','{}')")
    c.execute(sql)
    c.execute("UPDATE test_import_documents SET metadata='{}'::jsonb||jsonb_build_object('body_recovery',jsonb_build_object('method','pdftotext_layout'))")
    before=c.execute('SELECT * FROM test_import_documents').fetchone()
    c.execute("UPDATE eastmoney_reports_dedup SET content_text=NULL,content_hash='legacy-empty'")
    self.assertEqual(c.execute(sql).rowcount,0)
    after=c.execute('SELECT * FROM test_import_documents').fetchone();self.assertEqual(after,before)
    c.execute("UPDATE eastmoney_reports_dedup SET content_text='更新的完整正文',content_hash='new-version'")
    self.assertEqual(c.execute(sql).rowcount,1)
    after=c.execute('SELECT content_text,content_hash FROM test_import_documents').fetchone()
    self.assertEqual(after,{'content_text':'更新的完整正文','content_hash':'new-version'})
   finally:c.rollback()
if __name__=='__main__':unittest.main()
