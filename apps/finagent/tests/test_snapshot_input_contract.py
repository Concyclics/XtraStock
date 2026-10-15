import unittest
from scripts.analytics.snapshot_input_contract import freeze_contract


class FrozenInputTests(unittest.TestCase):
    def test_stale_score_cannot_borrow_new_feature_approval(self):
        f={'input_contract_version':'pit-input-v1','knowledge_mode':'system',
           'input_quality':{'strict_pit_ready':True},'updated_at':'2026-10-13T12:00:00Z'}
        score={'quality_status':'valid','input_contract_version':'pit-input-v1','source_feature_updated_at':'old',
               'source_factor_version':1,'version':1,'factor_name':'momentum_20d','percentile':.9}
        self.assertEqual(freeze_contract(f,[score])['eligible_score_names'],[])
        score['source_feature_updated_at']=f['updated_at']
        self.assertEqual(freeze_contract(f,[score])['eligible_score_names'],['momentum_20d'])
        score['version']=2
        self.assertEqual(freeze_contract(f,[score])['eligible_score_names'],[])

    def test_missing_feature_not_ready(self):
        self.assertFalse(freeze_contract(None,[])['strict_pit_ready'])
