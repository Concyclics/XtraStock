import sys,unittest,datetime as dt
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from corporate_action_versions import save_distribution,FIELDS,META


class FakeConnection:
    def __init__(self,old=None):self.old=old;self.calls=[];self.current=None
    def execute(self,sql,args):
        self.calls.append((sql,args));self.current=sql;return self
    def fetchone(self):
        return {'action_id':'new-action'} if self.current.startswith('INSERT') else self.old


def fixture():
    filing={'asset_id':'asset','announced_at':dt.datetime(2020,6,1,tzinfo=dt.timezone.utc),
      'available_at':dt.datetime(2026,10,14,tzinfo=dt.timezone.utc),'source_url':'https://static.cninfo.com.cn/example.PDF',
      'document_id':'doc','source_external_id':'123'}
    action={'action_type':'cash_dividend','record_date':dt.date(2020,6,9),'ex_date':dt.date(2020,6,10),
      'pay_date':dt.date(2020,6,10),'split_ratio':0.,'stock_dividend_ratio':0.,'cash_dividend':.1,'quality_status':'verified',
      'differential_distribution':False,'effective_cash_ratio_parsed':False,'effective_share_change_parsed':False,
      'declared_cash_dividend':.1,'declared_split_ratio':0.,'declared_stock_dividend_ratio':0.,'ex_date_recovered_from_pay_date':False}
    c=FakeConnection();save_distribution(c,filing,action,Path('/evidence.pdf'))
    args=c.calls[-1][1];old=dict(zip(FIELDS,args));old.update(revision_no=3,action_id='old-action',metadata=args[-1].obj)
    return filing,action,old


class VersionTests(unittest.TestCase):
    def test_new_amendment_requires_explicit_supersession_review(self):
        filing,action,old=fixture();filing['title']='因回购导致权益分派实施公告发生调整的公告';c=FakeConnection(old)
        with self.assertRaisesRegex(ValueError,'supersession review'):save_distribution(c,filing,action,Path('/evidence.pdf'))
        old['metadata']['supersession_reviewed']=True
        self.assertFalse(save_distribution(c,filing,action,Path('/evidence.pdf'))['changed'])

    def test_superseded_plan_cannot_be_reactivated_by_reparsing(self):
        filing,action,old=fixture();old['quality_status']='rejected';old['metadata']['superseded_by_external_id']='correction';c=FakeConnection(old)
        self.assertFalse(save_distribution(c,filing,action,Path('/evidence.pdf'))['changed'])
        self.assertFalse(any('INSERT' in s for s,_ in c.calls))

    def test_idempotent_reparse_does_not_overwrite_history(self):
        filing,action,old=fixture();c=FakeConnection(old)
        r=save_distribution(c,filing,action,Path('/evidence.pdf'))
        self.assertFalse(r['changed']);self.assertEqual(r['revision_no'],3)
        self.assertFalse(any('INSERT' in sql or 'UPDATE' in sql for sql,_ in c.calls))
    def test_date_correction_appends_and_uses_actual_availability(self):
        filing,action,old=fixture();action['ex_date']=dt.date(2020,6,11);c=FakeConnection(old)
        r=save_distribution(c,filing,action,Path('/evidence.pdf'))
        self.assertTrue(r['changed']);self.assertEqual(r['revision_no'],4)
        sql,args=c.calls[-1];self.assertIn('clock_timestamp()',sql);self.assertNotIn('UPDATE',sql)
        self.assertEqual(args[-1].obj['corrected_from_action_id'],'old-action')
        self.assertEqual(old['ex_date'],dt.date(2020,6,10))
    def test_new_declared_entitlement_creates_revision_even_if_reference_unchanged(self):
        filing,action,old=fixture();action['declared_cash_dividend']=.11;c=FakeConnection(old)
        self.assertTrue(save_distribution(c,filing,action,Path('/evidence.pdf'))['changed'])
    def test_same_source_reparse_does_not_upgrade_unverified(self):
        filing,action,old=fixture();old['quality_status']='unverified';c=FakeConnection(old)
        self.assertFalse(save_distribution(c,filing,action,Path('/evidence.pdf'))['changed'])


if __name__=='__main__':unittest.main()
