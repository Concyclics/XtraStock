"""Canonical identity and queue invalidation, with rollback-only SQL checks."""
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/search'))
import sync_document_search as search


class SearchIdentityTests(unittest.TestCase):
    def test_identity_provenance_survives_search_row_building(self):
        row={'id':'test','source_type':'research_report','source_name':'eastmoney',
             'identity_known_at':'2026-10-15T10:00:00Z','source_stock_code':'430418',
             'stock_code':'920418','title':'fixture','body':'original body'}
        built=search.build_search_row(row,6000)
        self.assertIn('920418',built['stock_codes'])
        self.assertEqual(built['metadata']['source_stock_code'],'430418')
        self.assertEqual(built['metadata']['identity_known_at'],row['identity_known_at'])

    @unittest.skipUnless(os.getenv('FINAGENT_TEST_DB')=='1','requires rollback-only local database fixture')
    def test_alias_trigger_invalidates_index_without_overwriting_worker_claim(self):
        with search.connect(admin=True) as conn:
            try:
                conn.execute("SET LOCAL statement_timeout='8s'")
                conn.execute("SET LOCAL lock_timeout='2s'")
                row=conn.execute('''SELECT a.old_code FROM market.research_security_code_aliases a
                  JOIN research.reports r ON r.stock_code=a.old_code GROUP BY a.old_code
                  ORDER BY count(*),a.old_code LIMIT 1''').fetchone()
                before={str(r['document_id']):r for r in conn.execute('''SELECT q.* FROM ops.document_index_queue q
                  JOIN research.reports r ON r.document_id=q.document_id WHERE r.stock_code=%s''',(row['old_code'],)).fetchall()}
                conn.execute('CREATE TEMP TABLE identity_fixture (LIKE market.research_security_code_aliases INCLUDING DEFAULTS) ON COMMIT DROP')
                conn.execute('CREATE TRIGGER fixture_identity AFTER INSERT ON identity_fixture FOR EACH ROW EXECUTE FUNCTION ops.enqueue_research_identity_search()')
                conn.execute('INSERT INTO identity_fixture SELECT * FROM market.research_security_code_aliases WHERE old_code=%s',(row['old_code'],))
                after=conn.execute('''SELECT q.* FROM ops.document_index_queue q JOIN research.reports r
                    ON r.document_id=q.document_id WHERE r.stock_code=%s''',(row['old_code'],)).fetchall()
                self.assertTrue(after)
                for item in after:
                    old=before.get(str(item['document_id']))
                    if old:
                        self.assertEqual(item['version'],old['version']+1)
                        self.assertEqual(item['status'],'running' if old['status']=='running' else 'pending')
                    else:self.assertEqual(item['status'],'pending')
            finally:conn.rollback()


if __name__=='__main__':unittest.main()
