"""Exercise actual cash books and independently reject mislabeled runs."""
import contextlib
import copy
import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from run_quant_cycle import evaluate_scores
from strategy_dataset import checksum, dump
from strategy_research import prepare_features
from verify_quant_cycle import verify
from tests.test_strategy_research import toy


class PortfolioCycleTests(unittest.TestCase):
    execution_audit = False
    horizon_study = False

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.data = toy(4, 330)
        self.data['cohort'] = np.asarray(['discovery', 'discovery', 'cross_section_reserve', 'cross_section_reserve'])
        self.features = prepare_features(self.data)
        self.scores = {'candidate': np.where(self.features['eligible'], np.asarray([4, 3, 2, 1]), np.nan)}
        members = {'all': self.data['symbols'].tolist(), 'discovery': self.data['symbols'][:2].tolist()}
        dump(self.path / 'COHORT_MEMBERS.json', members)
        self.spec = {'models': ['candidate'], 'allocations': {'one': {'size': 1}, 'three': {'size': 3}},
            'cohorts': ['all', 'discovery'], 'cost_bps_per_side': [15], 'test_years': [2021],
            'complete_test_years': [], 'evaluation_until': str(self.data['dates'][-1]),
            'capital': 123456., 'max_weight': 1., 'holding_buffer': 1., 'code': {},
            'portfolio_evaluation_contract': 1, 'cohort_members_sha256': checksum(self.path / 'COHORT_MEMBERS.json'),
            'accounting': 'Synthetic test', 'scope': 'Synthetic test',
            'primary_objective': {'net_cagr_above': .2, 'max_drawdown_at_most': .25,
                                  'period_win_rate_at_least': .55, 'positive_years_at_least': 3}}
        if self.execution_audit:
            self.spec['execution_audit'] = True
            self.spec['allocations']['one']['adjustment_fraction'] = .5
            self.spec['allocations']['three']['relative_no_trade_band'] = .25
        if self.horizon_study:
            self.spec['model_horizons'] = {'candidate': [5, 20]}
            self.spec['allocations']['one']['rebalance'] = 5
        dump(self.path / 'PROTOCOL.json', self.spec)
        self.protocol_hash = checksum(self.path / 'PROTOCOL.json')
        np.savez(self.path / 'PREDICTIONS.npz', **self.scores)
        dump(self.path / 'PREDICTION_RECEIPT.json', {'sha256': checksum(self.path / 'PREDICTIONS.npz'), 'protocol_sha256': self.protocol_hash})
        dump(self.path / 'TRAINING_FOLDS.json', [])
        with contextlib.redirect_stdout(io.StringIO()):
            evaluate_scores(self.data, self.features, self.scores, self.spec, self.path, 'test', self.protocol_hash)

    def load_simulation(self, cohort, model, allocation):
        with gzip.open(self.path / 'simulations' / f'{cohort}-cost15-{model}-{allocation}.json.gz', 'rt') as f:
            return json.load(f)

    def test_capital_and_all_universe_reach_actual_cash_books(self):
        self.assertEqual(verify(self.path)['simulations'], 8)
        full = self.load_simulation('all', 'equal_weight', 'benchmark')
        subset = self.load_simulation('discovery', 'equal_weight', 'benchmark')
        self.assertEqual(full['nav'][0], 123456.)
        self.assertEqual(subset['nav'][0], 123456.)
        self.assertEqual(full['rebalance_log'][0]['positions'], 4)
        self.assertEqual(subset['rebalance_log'][0]['positions'], 2)
        one = self.load_simulation('all', 'candidate', 'one')
        three = self.load_simulation('all', 'candidate', 'three')
        self.assertEqual(one['rebalance_log'][0]['positions'], 1)
        self.assertEqual(three['rebalance_log'][0]['positions'], 3)

    def test_updated_csv_fingerprint_cannot_hide_wrong_capital(self):
        path = self.path / 'COSTED_RESULTS.csv'
        table = pd.read_csv(path)
        table.loc[table.model == 'candidate', 'initial_cash'] = 10_000_000
        table.to_csv(path, index=False)
        contract_path = self.path / 'EXPERIMENT_CONTRACT.json'
        contract = json.loads(contract_path.read_text())
        contract['result_artifacts'][path.name] = checksum(path)
        dump(contract_path, contract)
        with self.assertRaisesRegex(ValueError, 'capital differ'):
            verify(self.path)

    def test_membership_violation_is_rejected_independently(self):
        result = self.load_simulation('discovery', 'candidate', 'one')
        result['rebalance_log'][0]['selected_symbols'] = [str(self.data['symbols'][2])]
        path = self.path / 'simulations/discovery-cost15-candidate-one.json.gz'
        with gzip.open(path, 'wt') as f:
            json.dump(result, f)
        with self.assertRaisesRegex(ValueError, 'declared cohort'):
            verify(self.path)

    def test_yearly_capital_label_must_match_cash_account(self):
        path = self.path / 'YEARLY_RESULTS.csv'
        table = pd.read_csv(path)
        table.loc[table.model == 'candidate', 'initial_cash'] = 10_000_000
        table.to_csv(path, index=False)
        with self.assertRaisesRegex(ValueError, 'Yearly capital or holdings'):
            verify(self.path)

    def test_unknown_cohort_does_not_silently_produce_cash_returns(self):
        spec = copy.deepcopy(self.spec)
        spec['cohorts'] = ['misspelled']
        with self.assertRaisesRegex(ValueError, 'unknown or empty'):
            evaluate_scores(self.data, self.features, self.scores, spec, self.path, 'test', self.protocol_hash)

    def test_allocation_cannot_silently_lower_reported_fee(self):
        spec = copy.deepcopy(self.spec)
        spec['allocations']['one']['cost_bps'] = 0
        with self.assertRaisesRegex(ValueError, 'declared cost or capital'):
            evaluate_scores(self.data, self.features, self.scores, spec, self.path, 'test', self.protocol_hash)


class AuditedPortfolioCycleTests(PortfolioCycleTests):
    execution_audit = True

    def test_changed_turnover_rule_is_rejected_independently(self):
        result = self.load_simulation('all', 'candidate', 'one')
        result['policy']['adjustment_fraction'] = .2
        with gzip.open(self.path / 'simulations/all-cost15-candidate-one.json.gz', 'wt') as f:
            json.dump(result, f)
        with self.assertRaisesRegex(ValueError, 'declared policy: adjustment_fraction'):
            verify(self.path)

    def test_changed_fill_fee_is_rejected_independently(self):
        result = self.load_simulation('all', 'candidate', 'one')
        result['execution_trace']['fills'][0]['fee'] += 1
        with gzip.open(self.path / 'simulations/all-cost15-candidate-one.json.gz', 'wt') as f:
            json.dump(result, f)
        with self.assertRaisesRegex(ValueError, 'Fill fee does not reconcile'):
            verify(self.path)

    def test_capacity_summary_cannot_hide_fill_diagnostics(self):
        path = self.path / 'COSTED_RESULTS.csv'
        table = pd.read_csv(path)
        table.loc[table.model == 'candidate', 'share_volume_breach'] += 1
        table.to_csv(path, index=False)
        with self.assertRaisesRegex(ValueError, 'summary differs from fill evidence'):
            verify(self.path)


class HorizonPortfolioCycleTests(PortfolioCycleTests):
    horizon_study = True

    def test_prediction_and_selection_horizons_survive_cashbook_reporting(self):
        table = pd.read_csv(self.path/'COSTED_RESULTS.csv')
        candidate = table[table.model == 'candidate']
        self.assertEqual(set(candidate.prediction_horizons), {'5+20'})
        self.assertEqual(set(candidate[candidate.allocation == 'one'].rebalance_sessions), {5})
        self.assertEqual(set(candidate[candidate.allocation == 'three'].rebalance_sessions), {20})
        self.assertTrue((table.horizon == 20).all())
        self.assertEqual(verify(self.path)['simulations'], 8)

    def test_wrong_prediction_label_is_rejected(self):
        path = self.path/'COSTED_RESULTS.csv'
        table = pd.read_csv(path)
        table.loc[table.model == 'candidate', 'prediction_horizons'] = '20'
        table.to_csv(path, index=False)
        with self.assertRaisesRegex(ValueError, 'horizon metadata'):
            verify(self.path)

    def test_default_selection_cadence_cannot_be_changed_even_with_matching_csv(self):
        result = self.load_simulation('all', 'candidate', 'three')
        result['policy']['rebalance'] = 5
        with gzip.open(self.path/'simulations/all-cost15-candidate-three.json.gz', 'wt') as f:
            json.dump(result, f)
        path = self.path/'COSTED_RESULTS.csv'
        table = pd.read_csv(path)
        table.loc[(table.model == 'candidate') & (table.allocation == 'three') & (table.cohort == 'all'), 'rebalance_sessions'] = 5
        table.to_csv(path, index=False)
        with self.assertRaisesRegex(ValueError, 'horizon metadata'):
            verify(self.path)


if __name__ == '__main__':
    unittest.main()
