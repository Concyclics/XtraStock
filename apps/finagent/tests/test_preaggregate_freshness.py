import datetime as dt
import unittest
from scripts.analytics.refresh_v3_preaggregates import upstream_quality
class FreshnessTests(unittest.TestCase):
 def test_rows_do_not_mask_failed_or_running_refresh(self):
  day=dt.date(2026,10,14)
  for status in ('failed','running',None):self.assertEqual(upstream_quality(100,day,day,status),'warn')
 def test_stale_success_is_not_fresh(self):
  self.assertEqual(upstream_quality(100,dt.date(2026,10,13),dt.date(2026,10,14),'ok'),'warn')
 def test_current_success_and_empty_output(self):
  day=dt.date(2026,10,14)
  self.assertEqual(upstream_quality(100,day,day,'ok'),'pass')
  self.assertEqual(upstream_quality(0,day,day,'ok'),'fail')
if __name__=='__main__':unittest.main()
