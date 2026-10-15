"""Real PostgreSQL acceptance; all fixture writes are rolled back."""
import datetime as dt
import os
import unittest
import uuid
from scripts.analytics.common import connect,jsonb
from scripts.analytics.paper_execution import step,default_policy
from scripts.analytics.run_quant_paper_daily import paper_bars
from scripts.pipeline.repair_windows import retain_older,finish_covered
from MMClaw.mmclaw.finance_db_helper import _load_holdings


@unittest.skipUnless(os.environ.get('RUN_QUANT_DB_TESTS')=='1','opt-in real DB rollback test')
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.c=connect(autocommit=False,attempts=1)
        self.c.execute("SET LOCAL statement_timeout='30s'")
        self.c.execute("SET LOCAL lock_timeout='3s'")

    def tearDown(self):
        self.c.rollback();self.c.close()

    def test_historical_holdings_do_not_use_future(self):
        name='upgrade-test-'+str(uuid.uuid4())
        pid=self.c.execute('INSERT INTO agent.portfolios(name) VALUES(%s) RETURNING portfolio_id',(name,)).fetchone()['portfolio_id']
        self.c.execute("INSERT INTO agent.portfolio_holdings(portfolio_id,stock_code,as_of_date,weight) VALUES(%s,'600000','2026-10-01',1),(%s,'600519','2026-10-12',1)",(pid,pid))
        rows,meta=_load_holdings(self.c,{'portfolio_name':name,'as_of_date':'2026-10-05'})
        self.assertEqual(rows,[{'stock_code':'600000','weight':1.0}])
        self.assertEqual(meta['holdings_cutoff'],'2026-10-05')

    def test_paper_bars_uses_real_schema_and_missing_rules(self):
        bars,_=paper_bars(self.c,['600519'],dt.date(2026,10,13))
        self.assertIn('600519',bars)
        # Merely having a price does not imply verified instrument rules.
        self.assertIn('lot_size',bars['600519'])

    def test_ledger_uniqueness_and_cash(self):
        import psycopg
        aid='upgrade-test-'+str(uuid.uuid4());oid=str(uuid.uuid4())
        sid=self.c.execute("SELECT snapshot_id FROM analytics.research_snapshot_runs WHERE status='complete' LIMIT 1").fetchone()['snapshot_id']
        policy=default_policy();policy['slippage_bps']=0
        self.c.execute('INSERT INTO quant.paper_accounts(account_id,initial_cash,policy,state,strategy_version) VALUES(%s,100000,%s,%s,%s)',(aid,jsonb(policy),jsonb({'cash':'100000','lots':[]}),'unit-rollback'))
        self.c.execute("INSERT INTO quant.paper_orders(order_id,account_id,snapshot_id,decision_at,session_date,stock_code,side,quantity) VALUES(%s,%s,%s,'2026-10-13T14:00:00Z','2026-10-14','600000','buy',1000)",(oid,aid,sid))
        bar={'open':10,'high':10,'low':10,'close':10,'amount':10000000,'quality':'verified','limit_up':11,'limit_down':9,'lot_size':100}
        result=step({'cash':'100000','lots':[]},'2026-10-14',{'600000':bar},[{'id':oid,'code':'600000','side':'buy','qty':1000,'session':'2026-10-14','decision_at':'2026-10-13T14:00:00Z'}],policy)
        f=result['fills'][0]
        self.c.execute("INSERT INTO quant.paper_fills(order_id,session_date,quantity,price,fees,gross) VALUES(%s,'2026-10-14',%s,%s,%s,%s)",(oid,f['qty'],f['price'],f['fees'],f['gross']))
        self.assertEqual(result['nav'],'99990.00')
        self.c.execute('SAVEPOINT duplicate_fill')
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.c.execute('INSERT INTO quant.paper_fills SELECT * FROM quant.paper_fills WHERE order_id=%s',(oid,))
        self.c.execute('ROLLBACK TO SAVEPOINT duplicate_fill')
        self.assertEqual(self.c.execute('SELECT count(*) n FROM quant.paper_fills WHERE order_id=%s',(oid,)).fetchone()['n'],1)

    def test_daily_correction_is_retained_then_completed(self):
        cutoff=dt.datetime.now(dt.timezone.utc)
        retain_older(self.c,'factor_daily',dt.date(2026,9,1),dt.date(2026,10,10),cutoff)
        row=self.c.execute('SELECT * FROM quant.repair_queue WHERE source_cutoff=%s',(cutoff,)).fetchone()
        self.assertEqual(row['status'],'pending');self.assertEqual(row['until_date'],dt.date(2026,10,9))
        finish_covered(self.c,'factor_daily',dt.date(2026,10,1),dt.date(2026,10,13),cutoff)
        self.assertEqual(self.c.execute('SELECT status FROM quant.repair_queue WHERE repair_id=%s',(row['repair_id'],)).fetchone()['status'],'pending')
        finish_covered(self.c,'factor_daily',dt.date(2026,9,1),dt.date(2026,10,13),cutoff)
        self.assertEqual(self.c.execute('SELECT status FROM quant.repair_queue WHERE repair_id=%s',(row['repair_id'],)).fetchone()['status'],'complete')


if __name__=='__main__':unittest.main()
