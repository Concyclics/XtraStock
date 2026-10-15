from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from strategy_research import Policy,backtest,portfolio_volatility
try:
    from tests.test_strategy_research import constant_case
except ModuleNotFoundError:
    from test_strategy_research import constant_case


class PortfolioRiskTests(unittest.TestCase):
    def test_opposing_returns_reduce_risk_without_manufacturing_zero_risk(self):
        r=np.tile([.03,-.03],40)
        together,_=portfolio_volatility(np.column_stack([r,r]),79,[0,1],[.5,.5])
        opposing,_=portfolio_volatility(np.column_stack([r,-r]),79,[0,1],[.5,.5])
        self.assertGreater(together,opposing)
        self.assertGreater(opposing,0)

    def test_future_returns_cannot_change_a_prior_risk_budget(self):
        r=np.random.default_rng(13).normal(0,.02,(100,3))
        before=portfolio_volatility(r,70,[0,2],[.4,.4])
        r[71:]*=1000
        self.assertEqual(before,portfolio_volatility(r,70,[0,2],[.4,.4]))

    def test_missing_returns_do_not_become_zero_volatility(self):
        r=np.full((60,2),np.nan);r[:20]=.01
        vol,observations=portfolio_volatility(r,59,[0,1],[.5,.5])
        self.assertTrue(np.isnan(vol))
        self.assertEqual(observations,20)

    def test_cash_weight_reduces_basket_risk_proportionally(self):
        r=np.random.default_rng(31).normal(0,.03,(80,2))
        full,_=portfolio_volatility(r,79,[0,1],[.5,.5])
        half,_=portfolio_volatility(r,79,[0,1],[.25,.25])
        self.assertAlmostEqual(half,full/2)

    def test_volatile_selected_stock_gets_smaller_budget_than_market_proxy(self):
        d,f=constant_case()
        f['returns'][:3,0]=[.04,-.04,.04]
        f['market_vol'][:]=.1
        kwargs=dict(initial_cash=10000,cost_bps=0,target_vol=.1,vol_window=3,vol_min_observations=3)
        old=backtest(d,f,Policy('medium_momentum',**kwargs),str(d['dates'][3]),str(d['dates'][5]),keep_trades=True)
        new=backtest(d,f,Policy('medium_momentum',vol_estimator='portfolio',**kwargs),str(d['dates'][3]),str(d['dates'][5]),keep_trades=True)
        self.assertEqual(old['rebalance_log'][0]['target_allocation'],1)
        self.assertLess(new['rebalance_log'][0]['target_allocation'],.25)
        self.assertLess(new['rebalance_log'][0]['buy_notional'],old['rebalance_log'][0]['buy_notional'])
        self.assertEqual(new['rebalance_log'][0]['risk_observations'],3)

    def test_insufficient_history_is_recorded_and_does_not_authorize_exposure(self):
        d,f=constant_case()
        result=backtest(d,f,Policy('medium_momentum',target_vol=.18,vol_estimator='portfolio'),
                        str(d['dates'][3]),str(d['dates'][5]),keep_trades=True)
        first=result['rebalance_log'][0]
        self.assertIsNone(first['estimated_annual_volatility'])
        self.assertEqual(first['target_allocation'],0)
        self.assertEqual(first['buy_notional'],0)


if __name__=='__main__':
    unittest.main()
