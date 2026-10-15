import unittest
import numpy as np
from scripts.analytics.forecast_baseline import fit,predict,calibration,scenario
from scripts.analytics.research_snapshot import select_watchlist

class ResearchUpgradeTests(unittest.TestCase):
    def test_training_imputation_does_not_use_test_statistics(self):
        model=fit([[1,2,3,4],[2,3,4,5],[3,4,5,6]],[.1,.2,.3])
        before=list(model['mean']);predict(model,[[999,np.nan,999,999]])
        self.assertEqual(model['mean'],before)
        self.assertTrue(np.isfinite(predict(model,[[np.nan]*4])).all())

    def test_unapproved_model_cannot_publish_price_or_return(self):
        params=fit([[1,2,3,4],[2,3,4,5],[3,4,5,6]],[.1,.2,.3])
        params['round_trip_bps']=50
        params['calibration']=[dict(lo=-100,hi=100,n=1000,residual_q10=-.1,residual_q90=.1,endpoint_above_10pct_rate=.6)]
        model=dict(parameters=params,validation={'passed':True},status='validated',horizon_days=5,model_version='test')
        result=scenario(model,[1,2,3,4],100,True)
        self.assertFalse(result['prediction_eligible'])
        self.assertIsNone(result['expected_net_return'])
        self.assertIsNone(result['price_equivalent_interval_80'])
        self.assertIsNone(result['first_touch_probability'])

    def test_old_or_stale_approved_models_and_missing_features_stay_blocked(self):
        import datetime as dt
        from scripts.analytics.forecast_baseline import MODEL_VERSION
        params=fit([[1,2,3,4],[2,3,4,5],[3,4,5,6]],[.1,.2,.3])
        params.update(round_trip_bps=50,calibration=[dict(lo=-100,hi=100,n=1000,residual_q10=-.1,residual_q90=.1,endpoint_above_10pct_rate=.6)])
        model=dict(parameters=params,validation={'passed':True,'input_contract_version':'pit-input-v1','strict_pit_ready':True},status='approved',approved_by='reviewer',
            approved_at='2026-10-12',decision_cutoff='2026-10-12',trained_through='2026-10-05',horizon_days=5,model_version=MODEL_VERSION)
        self.assertTrue(scenario(model,[1,2,3,4],100,True,dt.date(2026,10,12))['prediction_eligible'])
        without_contract={**model,'validation':{'passed':True}}
        self.assertFalse(scenario(without_contract,[1,2,3,4],100,True,dt.date(2026,10,12))['prediction_eligible'])
        self.assertFalse(scenario(model,[float('nan')]*4,100,True,dt.date(2026,10,12))['prediction_eligible'])
        self.assertFalse(scenario(model,[1,2,3,4],100,True,dt.date(2026,11,1))['prediction_eligible'])
        model['trained_through']='2025-01-01'
        self.assertFalse(scenario(model,[1,2,3,4],100,True,dt.date(2026,10,12))['prediction_eligible'])
        model['model_version']='old'
        self.assertFalse(scenario(model,[1,2,3,4],100,True,dt.date(2026,10,12))['prediction_eligible'])

    def test_hit_probability_differs_from_endpoint_probability(self):
        p=np.linspace(-.1,.1,1000);y=np.zeros(1000);hits=np.ones(1000)
        buckets=calibration(p,y,first_hits=hits)
        self.assertTrue(all(b['endpoint_above_10pct_rate']==0 for b in buckets))
        self.assertTrue(all(b['first_touch_probability']==1 for b in buckets))

    def test_missing_liquidity_and_partial_factor_coverage_are_excluded(self):
        item=dict(stock_code='000001',as_of_date='2026-10-09',theme=[{'evidence':'report'}],
          expected_factors=10,factors=[dict(dt='2026-10-09',percentile=.9,display_name='a',lifecycle_status='shadow')],
          market=dict(amount=None,quality_status='unverified',liquidity_proxy_percentile=.9))
        self.assertEqual(select_watchlist([item]),[])
        item['expected_factors']=1
        self.assertEqual(len(select_watchlist([item])),1)
        item['market']['liquidity_proxy_percentile']=None
        self.assertEqual(select_watchlist([item]),[])

class TargetTermsTests(unittest.TestCase):
    def test_only_explicit_forward_horizon_is_extracted(self):
        import datetime as dt
        import importlib.util,sys
        from pathlib import Path
        directory=Path(__file__).resolve().parents[1]/'scripts/analytics'
        sys.path.insert(0,str(directory))
        # The direct script import is kept local to avoid common-module collisions.
        from scripts.analytics.common import connect,jsonb
        import types
        spec=importlib.util.spec_from_file_location('terms_test',directory/'extract_research_target_price.py')
        module=importlib.util.module_from_spec(spec)
        from unittest.mock import patch
        with patch.dict(sys.modules,{'common':types.SimpleNamespace(connect=connect,jsonb=jsonb)}): spec.loader.exec_module(module)
        terms=module.target_terms('未来6个月目标价人民币100元。',100,dt.date(2026,1,31))
        self.assertEqual(terms['target_currency'],'CNY')
        self.assertEqual(terms['target_horizon_months'],6)
        self.assertEqual(terms['target_valid_until'],'2026-07-31')
        self.assertIsNone(module.target_terms('2026年6月报告目标价100元。',100,dt.date(2026,6,1))['target_horizon_months'])
