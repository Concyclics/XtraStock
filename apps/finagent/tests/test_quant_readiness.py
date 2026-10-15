import unittest
from scripts.analytics.quant_readiness import assess
class ReadinessTests(unittest.TestCase):
 def healthy(self):return {'calendar':{'sessions':60,'low_coverage_days':0},'features':{'strict':40},'lifecycle':{'verified_active':40},'invalid_legacy':0,'benchmark_days':60,'snapshot_current':True}
 def test_all_contracts_required(self):
  self.assertEqual(assess(self.healthy())['status'],'ready')
  for key,value in [('invalid_legacy',1),('benchmark_days',4),('snapshot_current',False)]:
   e=self.healthy();e[key]=value;self.assertEqual(assess(e)['status'],'blocked')
 def test_unknown_is_not_pass(self):self.assertEqual(assess({})['status'],'blocked')
 def test_old_features_not_enough(self):
  e=self.healthy();e['features']={'total':5000,'strict':0};self.assertEqual(assess(e)['status'],'blocked')
if __name__=='__main__':unittest.main()
