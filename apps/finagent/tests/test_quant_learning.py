import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from strategy_research import Policy, backtest, capped_weights, buffered_selection, metrics, prepare_features
from quant_learning import labels, research_features, fit_ridge, predict, block_bootstrap_cagr
try:
    from tests.test_strategy_research import constant_case, toy
except ModuleNotFoundError:
    from test_strategy_research import constant_case, toy


class CausalExecutionTests(unittest.TestCase):
    def test_close_range_volume_cannot_change_same_open_order(self):
        d, f = constant_case()
        p = Policy('medium_momentum', initial_cash=10000, cost_bps=0)
        original = backtest(d, f, p, str(d['dates'][2]), str(d['dates'][3]), keep_trades=True)
        d['volume'][2] = 0
        d['amount'][2] = 1
        d['high'][2] = d['low'][2] = 9
        d['close'][2] = 9
        f['valid'][2] = False
        modified = backtest(d, f, p, str(d['dates'][2]), str(d['dates'][3]), keep_trades=True)
        for key in ('buy_notional', 'sell_notional', 'selected_symbols', 'cash'):
            self.assertEqual(original['rebalance_log'][0][key], modified['rebalance_log'][0][key])
        self.assertEqual(modified['metrics']['blocked']['ex_post_no_volume_fill'], 1)
        self.assertEqual(modified['metrics']['blocked']['ex_post_capacity_breach'], 1)

    def test_prior_liquidity_is_the_sizing_cap(self):
        d, f = constant_case()
        d['amount'][1] = 100000
        d['amount'][2] = 100000000
        p = Policy('medium_momentum', initial_cash=10000, cost_bps=0)
        result = backtest(d, f, p, str(d['dates'][2]), str(d['dates'][3]), keep_trades=True)
        self.assertEqual(result['rebalance_log'][0]['buy_notional'], 1000)

    def test_weight_cap_keeps_unallocatable_cash(self):
        np.testing.assert_allclose(capped_weights([1, 1], .2), [.2, .2])
        w = capped_weights([100, 1, 1, 1], .4)
        self.assertAlmostEqual(w.sum(), 1)
        self.assertAlmostEqual(w.max(), .4)

    def test_buffer_retains_only_still_ranked_incumbents(self):
        scores = np.array([1, .9, .8, .7, .6])
        quantities = np.array([0, 0, 1, 1, 1])
        self.assertEqual(buffered_selection(scores, quantities, 2, 1.5).tolist(), [2, 0])
        self.assertEqual(buffered_selection(scores, quantities, 2, 1).tolist(), [0, 1])

    def test_risk_refresh_does_not_reselect_stocks_between_scheduled_dates(self):
        d, f = constant_case()
        f['market_on'][3:] = False
        p = Policy('medium_momentum', initial_cash=10000, cost_bps=0, risk_rebalance=1,
                   regime='lagged_market_trend', exposure_floor=.5)
        result = backtest(d, f, p, str(d['dates'][2]), str(d['dates'][5]), keep_trades=True)
        self.assertFalse(result['rebalance_log'][2]['selection_due'])
        self.assertEqual(result['rebalance_log'][2]['target_allocation'], .5)
        self.assertEqual(result['rebalance_log'][2]['sell_notional'], 5000)

    def test_cagr_uses_elapsed_calendar_year_not_assumed_252_sessions(self):
        result = metrics([100, 100, 121], ['2023-01-01', '2024-12-31'])
        expected = 1.21 ** (365.25 / 731) - 1
        self.assertAlmostEqual(result['cagr'], expected)
        self.assertNotAlmostEqual(result['cagr'], result['trading_252_cagr'])


class LearningTests(unittest.TestCase):
    def test_empty_training_rejected(self):
        with self.assertRaises(ValueError):
            fit_ridge(np.zeros((100, 2)), np.zeros(100), np.arange(100))

    def test_training_fit_does_not_depend_on_future_inputs(self):
        rng = np.random.default_rng(44)
        x = rng.normal(size=(1260, 3))
        y = x[:, 0] * .01
        model = fit_ridge(x, y, np.repeat(np.arange(126), 10))
        before = model['mean'].copy()
        a = predict(model, x[:10])
        predict(model, x[:10] * 1000000)
        np.testing.assert_array_equal(before, model['mean'])
        np.testing.assert_array_equal(a, predict(model, x[:10]))

    def test_missing_label_does_not_change_signal_eligibility(self):
        d = toy(25, 400)
        d['cohort'] = np.array(['discovery'] * 20 + ['cross_section_reserve'] * 5)
        f = prepare_features(d)
        eligibility = f['eligible'].copy()
        d['volume'][350, 0] = 0
        outcome, _ = labels(d, f, 20)
        self.assertTrue(np.isnan(outcome[340, 0]))
        np.testing.assert_array_equal(eligibility, f['eligible'])

    def test_reserve_labels_do_not_enter_training_benchmark(self):
        d = toy(25, 400)
        d['cohort'] = np.array(['discovery'] * 20 + ['cross_section_reserve'] * 5)
        f = prepare_features(d)
        _, before = labels(d, f)
        d['close'][:, 20:] *= 2
        _, after = labels(d, f)
        np.testing.assert_allclose(before[:, :20], after[:, :20], equal_nan=True)

    def test_missing_eps_has_separate_availability_flag(self):
        d = toy(25, 400)
        f = prepare_features(d)
        forecast = {'value': np.full_like(d['close'], np.nan), 'revision': np.full_like(d['close'], np.nan)}
        forecast['value'][300, 0] = .1
        matrix = research_features(d, f, forecast)['ridge_fundamentals']
        self.assertEqual(matrix[300, 0, -3], 1)
        self.assertEqual(matrix[300, 1, -3], 0)
        self.assertEqual(matrix[300, 1, -4], 0)

    def test_feature_prefix_invariance(self):
        d = toy(25, 400)
        a = research_features(d, prepare_features(d))
        shortened = {k: v[:320] if isinstance(v, np.ndarray) and len(v) == 400 else v for k, v in d.items()}
        b = research_features(shortened, prepare_features(shortened))
        for key in a:
            np.testing.assert_allclose(a[key][:320], b[key], equal_nan=True)

    def test_short_sample_does_not_emit_false_precision(self):
        result = block_bootstrap_cagr(np.linspace(100, 101, 20), ['2020-01-01']*19)
        self.assertEqual(result['status'], 'insufficient_independent_blocks')

    def test_bootstrap_uses_same_calendar_convention_as_point_estimate(self):
        dates=np.arange(np.datetime64('2020-01-01'),np.datetime64('2020-10-27')).astype(str)
        path=100*np.exp(np.arange(len(dates)+1)*.001)
        estimate=metrics(path,dates)['cagr']
        interval=block_bootstrap_cagr(path,dates,draws=20)
        self.assertAlmostEqual(interval['cagr_95pct_low'],estimate)
        self.assertAlmostEqual(interval['cagr_95pct_high'],estimate)


if __name__ == '__main__':
    unittest.main()
