import importlib.util,sys,unittest,sqlite3,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/pipeline'));sys.path.insert(0,str(ROOT/'Spider/eastmoney'))
from eastmoney.pg_storage import _asset_type_from_code
from market_gap_repair import validate_rows,ledger
class SecurityIdentityTests(unittest.TestCase):
    def test_market_disambiguates_same_code(self):
        self.assertEqual(_asset_type_from_code('0.000001'),'stock')
        self.assertEqual(_asset_type_from_code('1.000001'),'index')
        self.assertEqual(_asset_type_from_code('0.399001'),'index')
    def test_known_bse_and_csi_indices_not_stocks(self):
        for secid in ('0.899050','0.899601','2.930050','2.932000'):
            self.assertEqual(_asset_type_from_code(secid),'index')
        self.assertEqual(_asset_type_from_code('0.920001'),'stock')
    def test_provider_type_conflict_rejected(self):
        with self.assertRaisesRegex(ValueError,'identity conflict'):_asset_type_from_code('0.000001','index')
        with self.assertRaisesRegex(ValueError,'identity conflict'):_asset_type_from_code('0.899050','stock')
    def test_other_explicit_types_preserved(self):
        self.assertEqual(_asset_type_from_code('90.BK001','industry'),'industry')
        self.assertEqual(_asset_type_from_code('0.159001','fund'),'fund')
class GapRepairTests(unittest.TestCase):
    def row(self,day='2026-09-29',**kw):return dict(day=day,open='10',high='12',low='9',close='11',volume='100',**kw)
    def test_subset_does_not_invent_absent_requested_days(self):
        got=validate_rows([self.row()],{'2026-09-29','2026-09-30'})
        self.assertEqual(set(got),{'2026-09-29'})
    def test_invalid_ohlc_and_nonfinite_fail(self):
        for update in ({'high':'8'},{'close':'NaN'},{'volume':'-1'},{'low':'0'}):
            r=self.row();r.update(update)
            with self.assertRaises(ValueError):validate_rows([r],{'2026-09-29'})
    def test_conflicting_duplicate_rejected(self):
        a=self.row();b=self.row();b['close']='10.5'
        with self.assertRaisesRegex(ValueError,'conflicting duplicate'):validate_rows([a,b],{'2026-09-29'})
    def test_source_null_response_is_explicit_failure(self):
        with self.assertRaisesRegex(ValueError,'null/unavailable'):validate_rows(None,{'2026-09-29'})
    def test_unrequested_dates_excluded(self):
        self.assertEqual(validate_rows([self.row()],{'2026-09-30'}),{})
    def test_ledger_retains_checkpoint(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'queue.sqlite3';c=ledger(p)
            c.execute("INSERT INTO jobs(asset_id,secid,dates,status,attempts,updated_at) VALUES('x','0.000001','[\"2026-09-29\"]','partial',1,1)");c.commit();c.close()
            c=ledger(p);r=c.execute('SELECT * FROM jobs').fetchone();self.assertEqual(r['status'],'partial');self.assertEqual(r['attempts'],1);c.close()
if __name__=='__main__':unittest.main()
