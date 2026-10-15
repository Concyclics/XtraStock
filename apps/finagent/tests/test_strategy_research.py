import sys
from pathlib import Path
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from strategy_research import Policy, prepare_features, backtest, metrics, research_gate, paired_comparison, liquidity_amount


def toy(n=2,t=330):
    rng=np.random.default_rng(216)
    ret=rng.normal(.0003,.008,(t,n));close=20*np.exp(np.cumsum(ret,axis=0))
    opened=np.vstack([close[:1],close[:-1]])
    d={'dates':pd.bdate_range('2020-01-01',periods=t).strftime('%Y-%m-%d').to_numpy(),
       'symbols':np.asarray([f'{600000+i}' for i in range(n)]),'open':opened,'close':close,
       'high':np.maximum(close,opened)*1.001,'low':np.minimum(close,opened)*.999,
       'volume':np.full((t,n),10_000_000.),'amount':np.full((t,n),200_000_000.),
       'quality':np.ones((t,n),dtype=int),'adjustment_quality':np.ones((t,n),dtype=int),
       'total_return_factor':np.ones((t,n)),'split_factor':np.ones((t,n)), 'actions':[]}
    return d


def constant_case():
    d=toy(1,10)
    for k in ('open','close'):d[k][:]=10
    d['high'][:]=10.01;d['low'][:]=9.99
    f={'valid':np.ones((10,1),bool),'score':{'medium_momentum':np.ones((10,1))},
       'market_on':np.ones(10,bool),'market_vol':np.ones(10)*.2,'adv':d['amount'],
       'returns':np.zeros((10,1))}
    return d,f


class SimulatorTests(unittest.TestCase):
    def test_missing_amount_uses_lower_bound_with_canonical_lot_units(self):
        d,f=constant_case();d['volume'][:]=1000;d['amount'][:]=np.nan
        amount,estimated=liquidity_amount(d)
        f['adv']=amount.copy()
        self.assertAlmostEqual(amount[2,0],999000)
        self.assertTrue(estimated.all())
        r=backtest(d,f,Policy('medium_momentum',cost_bps=0,initial_cash=10000),str(d['dates'][2]),str(d['dates'][4]))
        self.assertGreater(r['metrics']['fill_count'],0)
        self.assertEqual(r['metrics']['amount_lower_bound_fill_days'],1)

    def test_missing_volume_does_not_fabricate_liquidity(self):
        d,f=constant_case();d['volume'][:]=np.nan;d['amount'][:]=np.nan
        amount,estimated=liquidity_amount(d)
        self.assertFalse(estimated.any());self.assertEqual(amount.sum(),0)

    def test_feature_prefix_invariance(self):
        d=toy(25,400);a=prepare_features(d)
        shortened={k:(v[:320] if isinstance(v,np.ndarray) and len(v)==400 else v) for k,v in d.items()}
        b=prepare_features(shortened)
        for family in a['score']:
            np.testing.assert_allclose(a['score'][family][:320],b['score'][family],equal_nan=True)
        np.testing.assert_array_equal(a['market_on'][:320],b['market_on'])

    def test_actual_turnover_fees_and_terminal_provision(self):
        d,f=constant_case();p=Policy('medium_momentum',cost_bps=10,initial_cash=10000)
        r=backtest(d,f,p,str(d['dates'][2]),str(d['dates'][4]))
        self.assertAlmostEqual(r['nav'][-1],9982.)
        self.assertAlmostEqual(r['metrics']['fees_paid'],9.)
        self.assertEqual(r['metrics']['closed_trade_count'],0)
        self.assertEqual(r['metrics']['open_position_count'],1)

    def test_unfilled_limit_gap_does_not_get_replaced(self):
        d,f=constant_case();d['open'][2]=11;d['close'][2]=11;d['high'][2]=11.01;d['low'][2]=10.99
        r=backtest(d,f,Policy('medium_momentum',initial_cash=10000),str(d['dates'][2]),str(d['dates'][3]))
        self.assertEqual(r['metrics']['fill_count'],0)
        self.assertEqual(r['nav'][-1],10000)
        self.assertEqual(r['metrics']['blocked']['buy'],1)

    def test_corporate_action_receivable_not_spendable_early(self):
        d,f=constant_case()
        for k in ('open','close'):d[k][3:]=4.5
        d['high'][3:]=4.51;d['low'][3:]=4.49
        d['actions']=[{'column':0,'ex_date':str(d['dates'][3]),'pay_date':str(d['dates'][6]),
           'action_type':'split','split_ratio':1.,'stock_dividend_ratio':0,'cash_dividend':1.,'quality_status':'verified'}]
        r=backtest(d,f,Policy('medium_momentum',cost_bps=0,initial_cash=10000,dividend_tax=0),str(d['dates'][2]),str(d['dates'][4]))
        self.assertEqual(r['nav'],[10000,10000,10000,10000])
        self.assertAlmostEqual(r['metrics']['terminal_receivables'],1000)

    def test_dividend_tax_cost_is_recognized(self):
        d,f=constant_case()
        for k in ('open','close'):d[k][3:]=9
        d['actions']=[{'column':0,'ex_date':str(d['dates'][3]),'pay_date':str(d['dates'][3]),
           'action_type':'cash_dividend','cash_dividend':1.,'quality_status':'verified'}]
        r=backtest(d,f,Policy('medium_momentum',cost_bps=0,initial_cash=10000),str(d['dates'][2]),str(d['dates'][4]))
        self.assertAlmostEqual(r['nav'][-1],9800)

    def test_missing_held_exit_is_preserved_and_flagged(self):
        d,f=constant_case();f['valid'][4:]=False;d['close'][4:]=np.nan
        r=backtest(d,f,Policy('medium_momentum',cost_bps=0,initial_cash=10000),str(d['dates'][2]),str(d['dates'][6]))
        self.assertEqual(r['nav'][-1],10000)
        self.assertEqual(r['metrics']['terminal_stale_value'],10000)
        self.assertEqual(r['metrics']['maximum_held_stale_sessions'],3)
        self.assertFalse(research_gate(r,paired_comparison(r,r))['checks']['no_terminal_stale_marks'])

    def test_current_signal_not_used_for_current_open(self):
        d,f=constant_case();f['score']['medium_momentum'][1]=np.nan
        r=backtest(d,f,Policy('medium_momentum',cost_bps=0,initial_cash=10000),str(d['dates'][2]),str(d['dates'][4]))
        self.assertEqual(r['metrics']['fill_count'],0)

    def test_open_loss_counts_in_total_return_without_closed_trade(self):
        d,f=constant_case();d['close'][4]=8
        r=backtest(d,f,Policy('medium_momentum',cost_bps=0,initial_cash=10000),str(d['dates'][2]),str(d['dates'][4]))
        self.assertAlmostEqual(r['metrics']['net_return'],-.2)
        self.assertEqual(r['metrics']['closed_trade_count'],0)

    def test_win_rate_uses_nonoverlapping_net_windows(self):
        days=pd.bdate_range('2020-01-01',periods=40).strftime('%Y-%m-%d')
        nav=np.concatenate([np.linspace(100,110,21),np.linspace(110,90,21)[1:]])
        m=metrics(nav,days,20)
        self.assertEqual(m['period_count'],2);self.assertEqual(m['period_win_rate'],.5)
        self.assertAlmostEqual(m['net_return'],-.1)

    def test_invalid_policy_does_not_enable_same_day_signal(self):
        d,f=constant_case()
        with self.assertRaises(ValueError):backtest(d,f,Policy('medium_momentum',signal_lag=0),str(d['dates'][2]),str(d['dates'][4]))


if __name__=='__main__':unittest.main()
