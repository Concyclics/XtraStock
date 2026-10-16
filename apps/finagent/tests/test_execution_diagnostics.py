import copy
import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from execution_diagnostics import audit_execution, classify_fill
from strategy_research import Policy, backtest
try:
    from tests.test_strategy_research import constant_case
except ModuleNotFoundError:
    from test_strategy_research import constant_case


class ExecutionDiagnosticTests(unittest.TestCase):
    def run_case(self, data=None, features=None):
        if data is None:
            data, features = constant_case()
        policy = Policy('medium_momentum', cost_bps=0, initial_cash=10000)
        return backtest(data, features, policy, str(data['dates'][2]), str(data['dates'][4]),
                        keep_trades=True, keep_fills=True)

    def test_trace_does_not_change_any_cashbook_field(self):
        data, features = constant_case()
        p = Policy('medium_momentum', cost_bps=15, initial_cash=10000)
        plain = backtest(data, features, p, str(data['dates'][2]), str(data['dates'][4]), keep_trades=True)
        traced = backtest(data, features, p, str(data['dates'][2]), str(data['dates'][4]), keep_trades=True, keep_fills=True)
        self.assertTrue(traced.pop('execution_trace')['fills'])
        self.assertEqual(plain, traced)

    def test_lower_bound_breach_is_not_share_volume_breach(self):
        data, features = constant_case()
        data['amount'][2] = np.nan; data['volume'][2] = 1000; data['low'][2] = 9
        result = self.run_case(data, features); audit = audit_execution(result)
        self.assertEqual(audit['counts']['legacy_notional_breach'], 1)
        self.assertEqual(audit['counts']['lower_bound_only_breach'], 1)
        self.assertEqual(audit['counts']['share_volume_breach'], 0)
        self.assertAlmostEqual(audit['flagged_fills'][0]['daily_share_participation'], .01)
        self.assertFalse(audit['execution_verified'])

    def test_current_volume_changes_diagnostic_never_prior_order_or_nav(self):
        data, features = constant_case()
        original = self.run_case(data, features)
        data['amount'][2] = np.nan; data['volume'][2] = 400
        changed = self.run_case(data, features); audit = audit_execution(changed)
        self.assertEqual(audit['counts']['share_volume_breach'], 1)
        for key in ('nav', 'trades', 'rebalance_log', 'turnover', 'exposure'):
            self.assertEqual(original[key], changed[key])
        self.assertEqual(original['execution_trace']['fills'][0]['shares'], changed['execution_trace']['fills'][0]['shares'])

    def test_unknown_volume_is_not_a_confirmed_volume_breach(self):
        data, features = constant_case()
        data['volume'][2] = np.nan; data['amount'][2] = np.nan
        audit = audit_execution(self.run_case(data, features))
        self.assertEqual(audit['counts']['unknown_daily_volume'], 1)
        self.assertEqual(audit['counts']['share_volume_breach'], 0)
        self.assertEqual(audit['counts']['unavailable_amount_breach'], 1)

    def test_observed_notional_and_share_participation_are_independent(self):
        result = self.run_case(); fill = result['execution_trace']['fills'][0]
        fill.update(daily_amount_observed=990000, daily_volume_lots=1000, daily_low=9, daily_high=11)
        label = classify_fill(fill, .01)
        self.assertTrue(label['observed_notional_breach'])
        self.assertFalse(label['share_volume_breach'])
        self.assertFalse(label['observed_amount_volume_inconsistent'])
        fill['daily_amount_observed'] = 10000
        self.assertTrue(classify_fill(fill, .01)['observed_amount_volume_inconsistent'])

    def test_split_dividend_fees_and_unsold_position_reconcile(self):
        data, features = constant_case()
        for key in ('open', 'close'):
            data[key][3:] = 4.5
        data['high'][3:] = 4.51; data['low'][3:] = 4.49
        data['actions'] = [{'column': 0, 'ex_date': str(data['dates'][3]), 'pay_date': str(data['dates'][6]),
            'action_type': 'split', 'split_ratio': 1., 'stock_dividend_ratio': 0., 'cash_dividend': 1., 'quality_status': 'verified'}]
        policy = Policy('medium_momentum', cost_bps=15, initial_cash=10000)
        result = backtest(data, features, policy, str(data['dates'][2]), str(data['dates'][4]), keep_fills=True)
        audit = audit_execution(result)
        self.assertAlmostEqual(audit['accounting']['net_pnl'], result['nav'][-1] - 10000)
        self.assertAlmostEqual(audit['asset_attribution'][0]['net_dividends'], 720)
        self.assertEqual(result['execution_trace']['terminal_positions'][0]['shares'], 1800)
        self.assertEqual(result['metrics']['terminal_receivables'], 720)

    def test_tampered_fee_share_ownership_and_nav_are_rejected(self):
        original = self.run_case()
        for change in ('fee', 'shares', 'nav', 'duplicate'):
            value = copy.deepcopy(original)
            if change == 'fee': value['execution_trace']['fills'][0]['fee'] += 1
            elif change == 'shares': value['execution_trace']['terminal_positions'][0]['shares'] += 1
            elif change == 'nav': value['nav'][-1] += 1
            else: value['execution_trace']['fills'].append(value['execution_trace']['fills'][0].copy())
            with self.subTest(change=change), self.assertRaises(ValueError):
                audit_execution(value)

    def test_cashbook_comparison_rejects_numerical_change(self):
        from run_execution_audit import compare_cashbook
        original = self.run_case()
        expected = {k: v for k, v in original.items() if k != 'execution_trace'}
        expected['protocol_sha256'] = 'external-receipt'
        compare_cashbook(original, expected)
        for field in ('nav', 'policy', 'metrics'):
            changed = copy.deepcopy(original)
            if field == 'nav': changed['nav'][-1] += .01
            elif field == 'policy': changed['policy']['cost_bps'] += 1
            else: changed['metrics']['fees_paid'] += .01
            with self.subTest(field=field), self.assertRaises(ValueError):
                compare_cashbook(changed, expected)


if __name__ == '__main__':
    unittest.main()
