import copy
import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from strategy_research import Policy, backtest, turnover_target
from execution_diagnostics import audit_execution
from run_execution_audit import compare_cashbook
from tests.test_strategy_research import constant_case


class TurnoverPolicyTests(unittest.TestCase):
    def test_disabled_adjustment_is_exactly_legacy_target(self):
        target = np.array([0., 100., 200.])
        actual = turnover_target(np.array([40., 110., 0.]), target, np.ones(3), 1000, 1, 1)
        self.assertIs(actual, target)

    def test_partial_target_preserves_full_exit(self):
        target = turnover_target(np.array([200., 100., 0.]), np.array([0., 200., 100.]),
                                 np.ones(3), 1000, 1, 1, .5, 0)
        np.testing.assert_array_equal(target, [0., 150., 50.])

    def test_relative_band_reduces_small_rebalances_and_never_blocks_exit(self):
        actual = turnover_target(np.array([90., 125., 50.]), np.array([100., 100., 0.]),
                                 np.ones(3), 1000, 1, 1, 1, .25)
        np.testing.assert_array_equal(actual, [90., 125., 0.])

    def test_risk_reductions_bypass_partial_adjustment_and_band(self):
        current, target = np.array([400., 400.]), np.array([300., 300.])
        for fraction, band in ((.5, 0), (1, .5), (.5, .5)):
            result = turnover_target(current, target, np.ones(2), 1000, .6, 1, fraction, band)
            np.testing.assert_array_equal(result, target)

    def test_concentration_reduction_is_not_deferred(self):
        result = turnover_target(np.array([600., 0.]), np.array([400., 400.]), np.ones(2), 1000, 1, .4, .5, .5)
        np.testing.assert_array_equal(result, [400., 200.])

    def test_suppressed_sells_cannot_create_excess_gross_target(self):
        result = turnover_target(np.array([120., 0.]), np.array([100., 100.]), np.ones(2), 1000, .2, 1, 1, .25)
        np.testing.assert_array_equal(result, [120., 80.])
        self.assertLessEqual(result.sum(), 200)

    def test_initial_partial_buy_and_later_full_exit_reconcile(self):
        data, features = constant_case()
        features['score']['medium_momentum'][6] = np.nan
        policy = Policy('medium_momentum', initial_cash=10000, cost_bps=15, rebalance=5,
                        adjustment_fraction=.5, relative_no_trade_band=.25)
        result = backtest(data, features, policy, str(data['dates'][2]), str(data['dates'][8]),
                          keep_trades=True, keep_fills=True)
        fills = result['execution_trace']['fills']
        self.assertEqual([(f['side'], f['shares']) for f in fills], [('buy', 500), ('sell', 500)])
        self.assertEqual(result['metrics']['open_position_count'], 0)
        self.assertAlmostEqual(result['nav'][-1], 9985)
        self.assertEqual(audit_execution(result)['accounting']['status'], 'passed')

    def test_new_defaults_only_are_compatible_with_old_control_metadata(self):
        data, features = constant_case()
        p = Policy('medium_momentum', initial_cash=10000)
        result = backtest(data, features, p, str(data['dates'][2]), str(data['dates'][4]), keep_trades=True)
        old = copy.deepcopy(result)
        old['policy'].pop('adjustment_fraction'); old['policy'].pop('relative_no_trade_band')
        compare_cashbook(result, old)
        changed = copy.deepcopy(result); changed['policy']['adjustment_fraction'] = .5
        with self.assertRaises(ValueError): compare_cashbook(changed, old)
        self.assertIn('adjustment_fraction', result['policy'])

    def test_invalid_policy_is_rejected_before_replay(self):
        data, features = constant_case()
        for kwargs in ({'adjustment_fraction': 0}, {'adjustment_fraction': np.nan},
                       {'relative_no_trade_band': 1}, {'relative_no_trade_band': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                backtest(data, features, Policy('medium_momentum', **kwargs), str(data['dates'][2]), str(data['dates'][4]))


if __name__ == '__main__':
    unittest.main()
