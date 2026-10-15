import unittest
from scripts.analytics.run_quant_paper_daily import targets,assert_session_continuity,DataGap


class TargetTests(unittest.TestCase):
    def sample(self):
        return {'strict':True,'lifecycle_quality':'verified','tradable':True,'amount':30_000_000,
                'listing_verified':True,'listed_sessions':100,'close':10,'feature_updated_at':'f1',
                'cohort_hash':'c1','factor_versions':dict.fromkeys(('momentum_20d','reversal_5d','low_volatility_20d'),1),
                'payload':{'stock_code':'600000','input_contract':{'version':'snapshot-input-v1','strict_pit_ready':True,
                    'feature_updated_at':'f1','eligible_score_names':['momentum_20d','reversal_5d','low_volatility_20d']},'factors':[
                    {'factor_name':name,'percentile':.8,'source_cohort_hash':'c1','source_factor_version':1} for name in
                    ('momentum_20d','reversal_5d','low_volatility_20d')]}}

    def test_normal_and_ipo(self):
        r=self.sample();self.assertEqual(len(targets([r])),1)
        self.assertEqual(targets([{**r,'listed_sessions':4}]),[])
        self.assertEqual(targets([{**r,'listing_verified':False}]),[])

    def test_duplicate_factor_cannot_fake_three_inputs(self):
        r=self.sample();r['payload']['factors']=[{'factor_name':'momentum_20d','percentile':.8}]*3
        self.assertEqual(targets([r]),[])

    def test_unverified_and_st(self):
        for change in ({'strict':False},{'is_st':True},{'lifecycle_quality':'unverified'}):
            self.assertEqual(targets([{**self.sample(),**change}]),[])

    def test_old_snapshot_cannot_borrow_repaired_feature(self):
        r=self.sample();r['payload'].pop('input_contract')
        self.assertEqual(targets([r]),[])

    def test_frozen_snapshot_invalidated_by_later_correction(self):
        for changed in ({'feature_updated_at':'f2'},{'cohort_hash':'c2'},
                        {'factor_versions':{'momentum_20d':2}}):
            self.assertEqual(targets([{**self.sample(),**changed}]),[])

    def test_missing_session_does_not_skip_corporate_action(self):
        assert_session_continuity('2026-10-13','2026-10-13',True)
        with self.assertRaises(DataGap):assert_session_continuity('2026-10-12','2026-10-13',True)
        with self.assertRaises(DataGap):assert_session_continuity(None,None,False)
