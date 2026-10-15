"""The weekly scheduler cannot turn stale/immature input into expensive work."""
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from run_weekly_factor_research import collect_gate,main,DAILY_SQL

TODAY=dt.date(2026,10,13)
class Result:
 def __init__(self,value):self.value=value
 def fetchone(self):return self.value
 def fetchall(self):return self.value
class Fake:
 def __init__(self,assets=100,feature_count=800,label_count=800,run_status='ok'):
  self.assets=assets;self.feature_count=feature_count;self.label_count=label_count;self.run_status=run_status;self.calls=[]
 def execute(self,q,p=None):
  self.calls.append(q)
  if 'weekly:expected' in q:return Result({'dt':TODAY})
  if 'weekly:daily' in q:return Result(dict(expected_dt=TODAY,scored_dt=TODAY if self.assets else None,scored_assets=self.assets,run_status=self.run_status,run_end_date=TODAY))
  if 'weekly:strict_dates' in q:return Result([{'dt':TODAY-dt.timedelta(days=i)} for i in range(self.feature_count)])
  if 'weekly:mature_dates' in q:return Result([{'dt':TODAY-dt.timedelta(days=i+25)} for i in range(self.label_count)])
  raise AssertionError(q)

class WeeklyGate(unittest.TestCase):
 def test_old_scores_cannot_bypass_even_with_ignore_flag(self):
  c=Fake(assets=0)
  r,w=collect_gate(c,TODAY,ignore_daily_gate=True)
  self.assertEqual(r['reasons'],['current_strict_scores_not_ready']);self.assertFalse(w)
  self.assertEqual(len(c.calls),2)
  self.assertIn('factor.valid_daily_scores_v1',DAILY_SQL)
  self.assertNotIn('FROM factor.daily_scores',DAILY_SQL)
 def test_small_current_cohort_is_blocked(self):
  r,_=collect_gate(Fake(assets=29),TODAY)
  self.assertEqual(r['status'],'blocked')
 def test_missing_daily_run_stops_before_history(self):
  c=Fake(run_status='failed');r,_=collect_gate(c,TODAY)
  self.assertEqual(r['reasons'],['daily_materialization_not_complete']);self.assertEqual(len(c.calls),2)
 def test_feature_history_stops_before_label_join(self):
  c=Fake(feature_count=799);r,_=collect_gate(c,TODAY)
  self.assertEqual(r['reasons'],['insufficient_strict_feature_history']);self.assertEqual(len(c.calls),3)
 def test_immature_labels_cannot_satisfy_independent_folds(self):
  r,w=collect_gate(Fake(label_count=799),TODAY)
  self.assertEqual(r['reasons'],['insufficient_mature_strict_label_history']);self.assertLess(len(w),3)
 def test_positive_control_has_three_purged_folds(self):
  c=Fake();r,w=collect_gate(c,TODAY)
  self.assertEqual(r['status'],'ready');self.assertEqual(len(w),3)
  self.assertIn('r.label_available_at_20d<=statement_timestamp()',c.calls[-1])
  self.assertIn("life.quality_status='verified'",c.calls[-1])
 def test_ignore_only_bypasses_crawler_status(self):
  r,_=collect_gate(Fake(run_status='failed'),TODAY,ignore_daily_gate=True)
  self.assertEqual(r['status'],'ready')
 def test_intraday_horizon_rejected(self):
  with self.assertRaises(ValueError):collect_gate(Fake(),TODAY,horizon=1)
  with patch('run_weekly_factor_research.run') as run:
   with self.assertRaises(SystemExit):main(['--horizon','1','--dry-run'])
   run.assert_not_called()
 def test_main_blocked_or_dryrun_never_launches_subprocess(self):
  for status,dry in [('blocked',False),('ready',True)]:
   with patch('run_weekly_factor_research.connect') as con,patch('run_weekly_factor_research.collect_gate') as gate,patch('run_weekly_factor_research.run') as run:
    gate.return_value=({'status':status,'reasons':[],'launched_commands':0},[])
    self.assertEqual(main(['--dry-run'] if dry else []),0);run.assert_not_called()

if __name__=='__main__':unittest.main()
