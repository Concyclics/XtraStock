"""Actual cash books, independent allocation math and frozen subset provenance."""
from dataclasses import asdict
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from run_portfolio_cycle import protocol
from strategy_dataset import checksum, dump
from strategy_research import Policy, backtest, prepare_features
from verify_portfolio_exposure import normalized_weights, expected_budget, audit_account
from tests.test_strategy_research import toy


class ExposureTests(unittest.TestCase):
    def test_weight_saturation_and_infeasible_full_investment(self):
        np.testing.assert_allclose(normalized_weights([9, 2, 1], .5), [.5, 1/3, 1/6])
        np.testing.assert_array_equal(normalized_weights([9, 1], .3), [.3, .3])
        with self.assertRaises(ValueError):
            normalized_weights([1, np.nan], .5)

    def test_independent_covariance_matches_closed_form_and_uses_only_prior_history(self):
        r=np.array([[.01,.02],[-.01,-.02],[.01,.02],[-.01,-.02],[.9,-.9]])
        f={'returns':r,'vol20':np.full_like(r,.1),'market_on':np.zeros(5,bool)}
        p=asdict(Policy('test',size=2,target_vol=.1,regime='lagged_market_trend',exposure_floor=.35,
            vol_estimator='portfolio',vol_window=4,vol_min_observations=4,covariance_shrinkage=.25))
        allocation,vol,count=expected_budget(p,f,3,[0,1])
        # Equal-weight basket is +/-1.5%; each marginal is perfectly correlated.
        expected=np.sqrt((.75*(4*.015**2/3)+.25*.25*(4*(.01**2+.02**2)/3))*252)
        self.assertAlmostEqual(vol,expected)
        self.assertAlmostEqual(allocation,.35*.1/expected)
        self.assertEqual(count,4)
        f['returns'][4]*=1000
        self.assertEqual((allocation,vol,count),expected_budget(p,f,3,[0,1]))
        f['returns'][:3]=np.nan
        self.assertEqual(expected_budget(p,f,3,[0,1]),(0.,None,1))

    def test_all_factorial_rules_reach_actual_books_and_tampering_is_detected(self):
        data=toy(4,330);f=prepare_features(data)
        f['score']['test']=np.where(f['eligible'],np.arange(1,5),np.nan)
        for weighting in ('equal','inverse_volatility'):
            for target,regime in ((None,'none'),(None,'lagged_market_trend'),(.24,'lagged_market_trend'),(.30,'lagged_market_trend')):
                p=Policy('test',size=3,initial_cash=1e6,max_weight=.5,weighting=weighting,
                    risk_rebalance=5,relative_no_trade_band=.25,target_vol=target,
                    regime=regime,exposure_floor=.35,vol_estimator='portfolio')
                result=backtest(data,f,p,str(data['dates'][260]),str(data['dates'][-1]),keep_trades=True)
                proof=audit_account(result,data,f)
                self.assertTrue(proof['target_rules_match'])
                self.assertEqual(proof['decisions'],14)
                corrupted=copy.deepcopy(result)
                corrupted['rebalance_log'][0]['target_allocation']+=.01
                with self.assertRaisesRegex(ValueError,'Target exposure'):
                    audit_account(corrupted,data,f)
                corrupted=copy.deepcopy(result)
                corrupted['rebalance_log'][1]['signal_date']=corrupted['rebalance_log'][1]['date']
                with self.assertRaisesRegex(ValueError,'wrong signal'):
                    audit_account(corrupted,data,f)

    def test_prediction_subset_retains_actual_horizons_and_rejects_mislabeling(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);ref=root/'reference';data=root/'market';ref.mkdir();data.mkdir()
            (data/'market.npz').write_bytes(b'synthetic provenance fixture')
            spec={'source':{'market.npz':checksum(data/'market.npz')},'code':{},'cost_bps_per_side':[15],
                'models':['short','long'],'model_horizons':{'short':[5],'long':[20]}}
            dump(ref/'PROTOCOL.json',spec);dump(ref/'TRAINING_FOLDS.json',[])
            np.savez(ref/'PREDICTIONS.npz',short=np.ones((1,1)),long=np.ones((1,1)))
            digest=checksum(ref/'PROTOCOL.json')
            dump(ref/'COMPLETE.json',{'protocol_sha256':digest})
            dump(ref/'PREDICTION_RECEIPT.json',{'protocol_sha256':digest,'sha256':checksum(ref/'PREDICTIONS.npz')})
            members=root/'members.json';dump(members,{'all':['synthetic']})
            study={'capital_levels':[1e6],'models':['short'],'model_horizons':{'short':[5]},
                'cohorts':['all'],'allocations':{'equal':{'size':1}},'exposure_audit':True}
            path=root/'study.json';dump(path,study)
            actual=protocol(data,ref,1e6,members,path)
            self.assertEqual(actual['model_horizons'],{'short':[5]})
            self.assertEqual(actual['reference_predictions_sha256'],checksum(ref/'PREDICTIONS.npz'))
            self.assertIn('verify_portfolio_exposure.py',actual['code'])
            study['model_horizons']['short']=[20];dump(path,study)
            with self.assertRaisesRegex(ValueError,'Reference prediction horizons'):
                protocol(data,ref,1e6,members,path)
            study['models']=['unknown'];study['model_horizons']={'unknown':[5]};dump(path,study)
            with self.assertRaisesRegex(ValueError,'Reference prediction horizons'):
                protocol(data,ref,1e6,members,path)
            self.assertEqual(json.loads((ref/'PROTOCOL.json').read_text()),spec)


if __name__=='__main__':
    unittest.main()
