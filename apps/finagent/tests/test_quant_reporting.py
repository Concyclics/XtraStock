"""Artifact-level checks: a profitable partial year cannot complete the gate."""
from pathlib import Path
import gzip
import json
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from strategy_dataset import checksum, dump
from strategy_research import metrics
from verify_quant_cycle import verify


class QuantReportingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        (self.path / 'simulations').mkdir()
        spec = {'models': ['candidate'], 'allocations': {'full': {}}, 'cohorts': ['discovery'],
                'cost_bps_per_side': [15], 'test_years': [2023, 2024, 2025, 2026],
                'complete_test_years': [2023, 2024, 2025], 'evaluation_until': '2026-10-15',
                'code': {}, 'primary_objective': {'net_cagr_above': .2, 'max_drawdown_at_most': .25,
                                                'period_win_rate_at_least': .55, 'positive_years_at_least': 3}}
        dump(self.path / 'PROTOCOL.json', spec)
        protocol_hash = checksum(self.path / 'PROTOCOL.json')
        np.savez(self.path / 'PREDICTIONS.npz', scores=np.zeros(1))
        dump(self.path / 'PREDICTION_RECEIPT.json', {'protocol_sha256': protocol_hash,
                                                  'sha256': checksum(self.path / 'PREDICTIONS.npz')})
        dump(self.path / 'COMPLETE.json', {'protocol_sha256': protocol_hash})
        dump(self.path / 'TRAINING_FOLDS.json', [])
        days = np.arange(np.datetime64('2023-01-01'), np.datetime64('2026-10-16'))
        dates = days[np.is_busday(days)].astype(str).tolist()
        counts = {year: sum(day.startswith(str(year)) for day in dates) for year in spec['test_years']}
        # Two positive complete years, one negative year and a positive YTD.
        growth = {2023: 1.4, 2024: .95, 2025: 1.5, 2026: 1.5}
        returns = np.asarray([growth[int(day[:4])] ** (1 / counts[int(day[:4])]) - 1 for day in dates])
        tables, years = [], []
        for name, allocation in [('equal_weight', 'benchmark'), ('momentum_low_vol', 'benchmark'), ('candidate', 'full')]:
            nav = np.r_[1., np.cumprod(1 + returns)] if name == 'candidate' else np.ones(len(dates) + 1)
            m = metrics(nav, dates)
            keys = {'model': name, 'allocation': allocation, 'cohort': 'discovery', 'cost': 15}
            tables.append({**keys, 'net_return': m['net_return'], 'net_cagr': m['cagr'],
                           'maximum_drawdown': m['maximum_drawdown'], 'period_win_rate': m['period_win_rate'],
                           'cagr_excess': m['cagr'], 'win_rate_difference': m['period_win_rate'],
                           'positive_years': 2 if name == 'candidate' else 0,
                           'positive_partial_years': 1 if name == 'candidate' else 0,
                           'complete_evaluation_years': 3, 'evaluation_start': dates[0], 'evaluation_end': dates[-1],
                           'numerical_target_pass': False})
            for year, value in m['annual'].items():
                section = [day for day in dates if day.startswith(year)]
                years.append({**keys, 'year': int(year), 'net_return': value['return'],
                              'is_partial_year': year == '2026', 'period_start': section[0], 'period_end': section[-1]})
            result = {'nav': nav.tolist(), 'dates': dates, 'protocol_sha256': protocol_hash,
                      'rebalance_log': [], 'metrics': {'minimum_cash': 0}}
            with gzip.open(self.path / 'simulations' / f'discovery-cost15-{name}-{allocation}.json.gz', 'wt') as f:
                json.dump(result, f)
        pd.DataFrame(tables).to_csv(self.path / 'COSTED_RESULTS.csv', index=False)
        pd.DataFrame(years).to_csv(self.path / 'YEARLY_RESULTS.csv', index=False)
        self.refresh_contract()

    def refresh_contract(self):
        dump(self.path / 'EXPERIMENT_CONTRACT.json', {'result_artifacts': {
            name: checksum(self.path / name) for name in ('COSTED_RESULTS.csv', 'YEARLY_RESULTS.csv')}})

    def change_candidate(self, column, value):
        path = self.path / 'COSTED_RESULTS.csv'
        table = pd.read_csv(path)
        table.loc[table.model == 'candidate', column] = value
        table.to_csv(path, index=False)
        self.refresh_contract()

    def test_two_positive_full_years_and_profitable_ytd_do_not_pass(self):
        table = pd.read_csv(self.path / 'COSTED_RESULTS.csv')
        self.assertGreater(table.loc[table.model == 'candidate', 'net_cagr'].iloc[0], .2)
        self.assertEqual(verify(self.path)['numerical_target_passes'], 0)

    def test_partial_year_cannot_be_counted_as_third_positive_full_year(self):
        self.change_candidate('positive_years', 3)
        with self.assertRaisesRegex(ValueError, 'Positive full-year count'):
            verify(self.path)

    def test_partial_year_label_must_match_the_declared_window(self):
        path = self.path / 'YEARLY_RESULTS.csv'
        table = pd.read_csv(path)
        table.loc[table.year == 2026, 'is_partial_year'] = False
        table.to_csv(path, index=False)
        self.refresh_contract()
        with self.assertRaisesRegex(ValueError, 'partial-year reporting'):
            verify(self.path)

    def test_updated_checksums_cannot_hide_a_false_gate_pass(self):
        self.change_candidate('numerical_target_pass', True)
        with self.assertRaisesRegex(ValueError, 'Numerical target gate'):
            verify(self.path)

    def test_claimed_baseline_advantage_is_recomputed(self):
        self.change_candidate('cagr_excess', .99)
        with self.assertRaisesRegex(ValueError, 'baseline advantage'):
            verify(self.path)


if __name__ == '__main__':
    unittest.main()
