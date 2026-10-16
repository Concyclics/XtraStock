import copy
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from nav_attribution import attribute_daily, summarize_window
from strategy_research import Policy, backtest
from tests.test_strategy_research import constant_case


class DailyAttributionTests(unittest.TestCase):
    def simulate(self, data, features, end=7):
        return backtest(data, features, Policy('medium_momentum', cost_bps=15, initial_cash=10000),
                        str(data['dates'][2]), str(data['dates'][end]), keep_trades=True, keep_fills=True)

    def test_flat_prices_only_fees_reduce_nav_including_terminal_provision(self):
        data, features = constant_case()
        result = self.simulate(data, features)
        detail = attribute_daily(result, data)
        self.assertEqual(detail['status'], 'passed')
        self.assertEqual(sum(r['holding_market_pnl'] for r in detail['daily']), 0.)
        self.assertAlmostEqual(sum(r['net_pnl'] for r in detail['daily']), result['nav'][-1]-10000)
        self.assertGreater(detail['daily'][-1]['liquidation_provision'], 0.)

    def test_split_and_delayed_dividend_payment_do_not_create_profit_twice(self):
        data, features = constant_case()
        for name in ('open', 'close'): data[name][3:] = 4.5
        data['high'][3:] = 4.51; data['low'][3:] = 4.49
        data['actions'] = [{'column': 0, 'ex_date': str(data['dates'][3]), 'pay_date': str(data['dates'][6]),
            'action_type': 'split', 'split_ratio': 1., 'stock_dividend_ratio': 0., 'cash_dividend': 1., 'quality_status': 'verified'}]
        detail = attribute_daily(self.simulate(data, features), data)
        ex, paid = detail['daily'][1], detail['daily'][4]
        self.assertEqual(ex['holding_market_pnl'], 0.)
        self.assertEqual(ex['action_mark_pnl'], -900.)
        self.assertEqual(ex['net_dividend'], 720.)
        self.assertEqual(ex['net_pnl'], -180.)
        self.assertEqual(paid['dividends_paid'], 720.)
        self.assertEqual(paid['net_pnl'], 0.)
        self.assertEqual(paid['receivable'], 0.)

    def test_unpaid_entitlement_remains_receivable_at_end(self):
        data, features = constant_case()
        data['actions'] = [{'column': 0, 'ex_date': str(data['dates'][3]), 'pay_date': str(data['dates'][9]),
            'action_type': 'cash_dividend', 'cash_dividend': 1., 'quality_status': 'verified'}]
        detail = attribute_daily(self.simulate(data, features), data)
        self.assertEqual(detail['daily'][-1]['receivable'], 720.)

    def test_missing_close_keeps_mark_until_valid_observation(self):
        data, features = constant_case()
        data['close'][3] = np.nan; data['quality'][3] = 0; features['valid'][3] = False
        detail = attribute_daily(self.simulate(data, features), data)
        self.assertEqual(detail['daily'][1]['net_pnl'], 0.)
        self.assertEqual(detail['asset_days'][1]['marked_value'], 9000.)

    def test_entry_day_move_belongs_to_trade_to_close_pnl(self):
        data, features = constant_case()
        data['close'][2:] = 10.3; data['high'][2:] = 10.31
        detail = attribute_daily(self.simulate(data, features), data)
        self.assertAlmostEqual(detail['daily'][0]['trade_to_close_pnl'], 270.)
        self.assertEqual(detail['daily'][0]['holding_market_pnl'], 0.)

    def test_window_is_exact_inclusive_sessions_with_prior_nav_denominator(self):
        data, features = constant_case()
        detail = attribute_daily(self.simulate(data, features), data)
        window = summarize_window(detail, str(data['dates'][3]), str(data['dates'][6]))
        self.assertEqual(window['sessions'], 4)
        self.assertEqual(window['net_pnl'], 0.)
        self.assertEqual(window['nav_start'], detail['daily'][0]['nav_after'])
        with self.assertRaises(ValueError): summarize_window(detail, '1900-01-01', str(data['dates'][6]))

    def test_intermediate_nav_exposure_checkpoint_and_action_tampering_rejected(self):
        data, features = constant_case(); original = self.simulate(data, features)
        for kind in ('nav', 'exposure', 'cash', 'action', 'calendar'):
            value = copy.deepcopy(original)
            if kind == 'nav': value['nav'][2] += 1
            elif kind == 'exposure': value['exposure'][2] += .01
            elif kind == 'cash': value['rebalance_log'][0]['cash'] += 1
            elif kind == 'action':
                value['execution_trace']['corporate_actions'].append({'date': value['dates'][1], 'symbol': '600000',
                    'shares_before': 900., 'extra_shares_per_share': 0., 'net_dividend': 0., 'payment_index': 3, 'quality': 'verified'})
            else: value['dates'].pop(2)
            with self.subTest(kind=kind), self.assertRaises(ValueError): attribute_daily(value, data)


if __name__ == '__main__':
    unittest.main()
