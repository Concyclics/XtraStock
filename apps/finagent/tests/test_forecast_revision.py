"""Real PostgreSQL revision contract, with all test mutations rolled back."""
import os
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect


@unittest.skipUnless(os.getenv('FINAGENT_TEST_DB') == '1', 'requires local test DB authorization')
class ForecastRevisionDBTest(unittest.TestCase):
    def test_noop_revision_preservation_and_append_only_history(self):
        c=connect(admin=True,autocommit=False)
        try:
            c.execute("SET LOCAL lock_timeout='5s'")
            old=c.execute("SELECT * FROM research.report_forecasts WHERE source='eastmoney:dated_text_v1' AND eps>0 ORDER BY id LIMIT 1 FOR UPDATE").fetchone()
            self.assertIsNotNone(old)
            n=c.execute('SELECT count(*) n FROM research.report_forecast_revision_history WHERE forecast_id=%s',(old['id'],)).fetchone()['n']
            self.assertEqual(c.execute('UPDATE research.report_forecasts SET eps=eps WHERE id=%s',(old['id'],)).rowcount,0)
            c.execute('UPDATE research.report_forecasts SET eps=eps+0.000001 WHERE id=%s',(old['id'],))
            new=c.execute('SELECT eps,updated_at FROM research.report_forecasts WHERE id=%s',(old['id'],)).fetchone()
            self.assertGreater(new['updated_at'],old['updated_at'])
            rows=c.execute('SELECT record FROM research.report_forecast_revision_history WHERE forecast_id=%s ORDER BY revision_id',(old['id'],)).fetchall()
            self.assertEqual(len(rows),n+1)
            self.assertEqual(float(rows[-1]['record']['eps']),float(old['eps']))
            with self.assertRaisesRegex(Exception,'append-only'):
                with c.transaction():
                    c.execute('DELETE FROM research.report_forecast_revision_history WHERE forecast_id=%s',(old['id'],))
        finally:
            c.rollback()
            c.close()


if __name__=='__main__': unittest.main()
