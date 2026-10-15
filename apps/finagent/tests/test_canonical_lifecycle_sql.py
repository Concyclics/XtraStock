"""Run exact canonical coverage SQL against rollback-only PostgreSQL temporary tables."""
import datetime as dt,importlib.util,os,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts/analytics'))
from scripts.analytics.common import connect
from sync_canonical_daily_bars_v2 import COVERAGE_SQL
@unittest.skipUnless(os.environ.get('FINAGENT_DB_TESTS')=='1','explicit PostgreSQL temporary-table integration run')
class LifecycleCoverageSQLTests(unittest.TestCase):
 def setUp(self):
  self.c=connect(autocommit=False);self.c.execute("SET LOCAL statement_timeout='25s'")
  self.c.execute("CREATE TEMP TABLE test_assets(asset_id int PRIMARY KEY,asset_type text,metadata jsonb)")
  self.c.execute("CREATE TEMP TABLE test_lifecycle(lifecycle_version_id int,asset_id int,valid_from date,valid_to date,available_at timestamptz,lifecycle_status text,is_tradable bool,is_suspended bool,quality_status text DEFAULT 'unverified')")
  self.c.execute("CREATE TEMP TABLE test_bars(asset_id int,dt date,quality_status text,is_fallback bool)")
  self.c.execute("INSERT INTO test_assets VALUES(1,'stock','{}')")
  self.sql=COVERAGE_SQL.replace('market.assets','test_assets').replace('market.asset_lifecycle_versions','test_lifecycle').replace('market.canonical_bars_daily_v2','test_bars')
 def tearDown(self):self.c.rollback();self.c.close()
 def state(self,id,status,at='2026-10-13 10:00:00+00',start='2026-08-01',end=None,quality='unverified'):
  self.c.execute('INSERT INTO test_lifecycle VALUES(%s,1,%s,%s,%s,%s,%s,%s,%s)',(id,start,end,at,status,status=='active',status=='suspended',quality))
 def check(self):return self.c.execute(self.sql,{'expected_date':dt.date(2026,9,29),'lifecycle_cutoff':dt.datetime(2026,10,13,12,tzinfo=dt.timezone.utc)}).fetchone()
 def test_latest_suspension_overrides_old_active(self):
  self.state(1,'active');self.state(2,'suspended','2026-10-13 11:00:00+00');r=self.check();self.assertEqual(r['eligible_assets'],0);self.assertEqual(r['unknown_lifecycle_assets'],0)
 def test_future_revision_cannot_override_known_state(self):
  self.state(1,'active');self.state(2,'unknown','2026-10-13 13:00:00+00');r=self.check();self.assertEqual(r['eligible_assets'],1);self.assertEqual(r['unknown_lifecycle_assets'],0)
 def test_latest_active_removes_obsolete_unknown_failure(self):
  self.state(1,'unknown');self.state(2,'active','2026-10-13 11:00:00+00');self.c.execute("INSERT INTO test_bars VALUES(1,'2026-09-29','verified',false)");r=self.check();self.assertEqual(r['unknown_actual_assets'],0);self.assertEqual(r['coverage_pct'],100)
 def test_equal_availability_has_deterministic_revision_tie_break(self):
  self.state(1,'active');self.state(2,'suspended');self.assertEqual(self.check()['eligible_assets'],0)
 def test_missing_known_version_is_unknown_not_eligible(self):
  self.state(1,'active','2026-10-13 13:00:00+00');self.c.execute("INSERT INTO test_bars VALUES(1,'2026-09-29','verified',false)");r=self.check();self.assertEqual(r['unknown_actual_assets'],1);self.assertEqual(r['eligible_assets'],0)
 def test_validity_interval_filters_irrelevant_suspension(self):
  self.state(1,'active');self.state(2,'suspended','2026-10-13 11:00:00+00',start='2026-09-30',end='2026-09-30');self.assertEqual(self.check()['eligible_assets'],1)
 def test_price_on_suspended_day_is_a_conflict(self):
  self.state(1,'suspended');self.c.execute("INSERT INTO test_bars VALUES(1,'2026-09-29','verified',false)");r=self.check();self.assertEqual(r['nontradable_actual_assets'],1);self.assertEqual(r['actual_assets'],0)
 def test_alias_excluded_from_every_denominator(self):
  self.c.execute("UPDATE test_assets SET metadata='{\"identity_alias_of\":\"canonical\"}'");self.state(1,'active');self.assertEqual(self.check()['eligible_assets'],0)
 def test_later_inference_cannot_override_reviewed_suspension(self):
  self.state(1,'suspended',quality='verified');self.state(2,'active','2026-10-13 11:00:00+00');self.assertEqual(self.check()['eligible_assets'],0)
 def test_future_reviewed_evidence_does_not_leak(self):
  self.state(1,'active');self.state(2,'suspended','2026-10-13 13:00:00+00',quality='verified');self.assertEqual(self.check()['eligible_assets'],1)
 def test_later_reviewed_conflict_blocks_prior_verification(self):
  self.state(1,'active',quality='verified');self.state(2,'active','2026-10-13 11:00:00+00',quality='conflict');r=self.check();self.assertEqual(r['eligible_assets'],0);self.assertEqual(r['unknown_lifecycle_assets'],1)
 def test_expired_reviewed_suspension_cannot_override_resumption(self):
  self.state(1,'suspended',end='2026-09-28',quality='verified');self.state(2,'active');self.assertEqual(self.check()['eligible_assets'],1)
if __name__=='__main__':unittest.main()
