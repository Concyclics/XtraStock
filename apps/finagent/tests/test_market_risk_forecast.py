"""Causality, missing-data semantics and actual risk-budget consumption."""
import copy
import contextlib
import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from market_risk_forecast import (DEFAULT_CONFIG, FORECAST_NAMES, market_inputs, forward_variance,
    ewma_variance, forecast, prediction_metrics, build_artifacts, verify_artifacts, verify_risk_budget)
from strategy_research import Policy, backtest, prepare_features
from strategy_dataset import checksum, dump
from tests.test_strategy_research import toy, constant_case
from run_quant_cycle import evaluate_scores
from verify_quant_cycle import verify


def inputs():
    rng = np.random.default_rng(19)
    return pd.DataFrame({'date': pd.bdate_range('2020-01-01', periods=950).strftime('%Y-%m-%d'),
        'market_return': rng.normal(0., .01, 950), 'market_members': 100, 'market_on': 1})


class MarketRiskForecastTests(unittest.TestCase):
    def setUp(self):
        self.inputs = inputs()
        self.spec = {'market_risk_model': DEFAULT_CONFIG, 'primary_since': '2020-06-01',
            'test_years': [2022, 2023], 'evaluation_until': str(self.inputs.date.iloc[-1]), 'source': {}}

    def run_forecast(self, values):
        return forecast(values, DEFAULT_CONFIG, self.spec['primary_since'], '2022-01-01', self.spec['evaluation_until'])

    def test_future_price_and_label_changes_cannot_change_prior_forecasts(self):
        original, _ = self.run_forecast(self.inputs)
        for cut in (580, 780):
            changed = self.inputs.copy()
            changed.loc[cut+1:, 'market_return'] *= 5
            actual, _ = self.run_forecast(changed)
            np.testing.assert_array_equal(original.loc[:cut, FORECAST_NAMES], actual.loc[:cut, FORECAST_NAMES])

    def test_every_training_label_matures_strictly_before_the_first_signal(self):
        _, folds = self.run_forecast(self.inputs)
        self.assertTrue(folds)
        for fold in folds:
            self.assertEqual(fold['status'], 'fitted')
            self.assertLess(fold['train_label_last'], fold['first_signal'])
            self.assertGreaterEqual(fold['train_signal_first'], self.spec['primary_since'])
            first = fold['prediction_signal_indices'][0]
            self.assertTrue(all(i+20 < first and i >= first-756 for i in fold['training_signal_indices']))

    def test_unfinished_and_missing_windows_are_not_extended_or_zero_filled(self):
        values = np.arange(50, dtype=float)/1000
        values[25] = np.nan
        out = forward_variance(values, 20)
        self.assertAlmostEqual(out[0], np.mean(values[1:21]**2))
        self.assertTrue(np.isnan(out[5:25]).all())
        self.assertTrue(np.isfinite(out[25]))
        self.assertTrue(np.isnan(out[-20:]).all())
        ewma = ewma_variance(values)
        self.assertTrue(np.isnan(ewma[25:47]).all())
        self.assertTrue(np.isfinite(ewma[47]))

    def test_low_breadth_is_missing_and_membership_is_prior_eligible(self):
        data = toy(25, 330)
        features = prepare_features(data)
        original = market_inputs(data, features)
        self.assertTrue(original.loc[original.market_members < 20, 'market_return'].isna().all())
        changed = copy.deepcopy(features)
        changed['eligible'][310] = False
        actual = market_inputs(data, changed)
        self.assertEqual(original.market_return.iloc[310], actual.market_return.iloc[310])
        self.assertTrue(np.isnan(actual.market_return.iloc[311]))

    def test_only_completed_same_support_outcomes_are_scored(self):
        table, _ = self.run_forecast(self.inputs)
        result = prediction_metrics(table)
        all_rows = result[(result.year == 'all') & (result.allocation == 'risk_prediction_all_matured')]
        self.assertEqual(len(all_rows), 4)
        self.assertEqual(all_rows.observations.nunique(), 1)
        self.assertEqual(int(all_rows.observations.iloc[0]), int(table.evaluation_signal.sum())-19)
        blocks = result[(result.year == 'all') & (result.allocation == 'risk_prediction_nonoverlap20')]
        self.assertLessEqual(int(blocks.observations.max()), int(all_rows.observations.max())/20+1)

    def test_early_history_cannot_be_used_to_bypass_minimum_training_dates(self):
        table, folds = forecast(self.inputs, DEFAULT_CONFIG, '2021-12-01', '2022-01-01', self.spec['evaluation_until'])
        self.assertEqual(folds[0]['status'], 'insufficient_matured_history')
        self.assertTrue(table.loc[folds[0]['prediction_signal_indices'], 'market_har20'].isna().all())

    def test_artifacts_recompute_folds_predictions_and_scores(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            build_artifacts(path, self.inputs, self.spec, 'test')
            self.assertEqual(len(verify_artifacts(path, self.spec, 'test')), len(self.inputs))
            p = path/'MARKET_RISK_FORECASTS.csv'
            table = pd.read_csv(p)
            table.loc[table.evaluation_signal, 'market_har20'] *= 2
            table.to_csv(p, index=False)
            receipt = json.loads((path/'MARKET_RISK_RECEIPT.json').read_text())
            receipt['artifacts'][p.name] = checksum(p)
            dump(path/'MARKET_RISK_RECEIPT.json', receipt)
            with self.assertRaisesRegex(ValueError, 'causal reconstruction'):
                verify_artifacts(path, self.spec, 'test')

    def test_actual_cash_book_uses_prior_forecast_and_missing_risk_is_explicit(self):
        data, features = constant_case()
        features['market_risk_forecasts'] = {'market_har20': np.full(10, .4)}
        features['market_risk_forecasts']['market_har20'][3:] = .1
        policy = Policy('medium_momentum', initial_cash=10000, cost_bps=0, target_vol=.2,
                        vol_estimator='market_har20', risk_rebalance=1)
        result = backtest(data, features, policy, str(data['dates'][3]), str(data['dates'][5]), keep_trades=True)
        self.assertEqual(result['rebalance_log'][0]['target_allocation'], .5)
        self.assertEqual(result['rebalance_log'][1]['target_allocation'], 1.)
        risks = {str(day): {'market_har20': features['market_risk_forecasts']['market_har20'][i], 'market_on': True}
                 for i, day in enumerate(data['dates'])}
        verify_risk_budget(result, risks)
        result['rebalance_log'][0]['estimated_annual_volatility'] = .1
        with self.assertRaisesRegex(ValueError, 'different forecast'):
            verify_risk_budget(result, risks)
        features['market_risk_forecasts']['market_har20'][:] = np.nan
        missing = backtest(data, features, policy, str(data['dates'][3]), str(data['dates'][5]), keep_trades=True)
        self.assertEqual(missing['metrics']['fill_count'], 0)
        self.assertIsNone(missing['rebalance_log'][0]['estimated_annual_volatility'])

    def test_missing_or_invalid_forecast_is_rejected_without_fallback(self):
        data, features = constant_case()
        policy = Policy('medium_momentum', target_vol=.2, vol_estimator='market_har20')
        with self.assertRaisesRegex(ValueError, 'missing market risk'):
            backtest(data, features, policy, str(data['dates'][3]), str(data['dates'][5]))
        features['market_risk_forecasts'] = {'market_har20': np.full(10, -1.)}
        with self.assertRaisesRegex(ValueError, 'invalid market risk'):
            backtest(data, features, policy, str(data['dates'][3]), str(data['dates'][5]))

    def test_complete_account_verifier_rejects_risk_and_timing_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            data = toy(25, 900)
            data['cohort'] = np.asarray(['discovery']*25)
            features = prepare_features(data)
            scores = {'candidate': np.where(features['eligible'], np.arange(25), np.nan)}
            dump(out/'COHORT_MEMBERS.json', {'all': data['symbols'].tolist()})
            spec = {**self.spec, 'evaluation_until': str(data['dates'][-1]), 'primary_since': '2021-01-01',
                'models': ['candidate'], 'allocations': {'har': {'size': 5, 'target_vol': .18,
                    'vol_estimator': 'market_har20', 'risk_rebalance': 5}},
                'cohorts': ['all'], 'cost_bps_per_side': [15], 'complete_test_years': [2022],
                'capital': 1000000., 'max_weight': .25, 'holding_buffer': 1., 'code': {},
                'portfolio_evaluation_contract': 1, 'cohort_members_sha256': checksum(out/'COHORT_MEMBERS.json'),
                'accounting': 'Synthetic risk integration', 'scope': 'Synthetic', 'execution_audit': True,
                'primary_objective': {'net_cagr_above': .2, 'max_drawdown_at_most': .25,
                    'period_win_rate_at_least': .55, 'positive_years_at_least': 3}}
            dump(out/'PROTOCOL.json', spec)
            ph = checksum(out/'PROTOCOL.json')
            np.savez(out/'PREDICTIONS.npz', **scores)
            dump(out/'PREDICTION_RECEIPT.json', {'sha256': checksum(out/'PREDICTIONS.npz'), 'protocol_sha256': ph})
            dump(out/'TRAINING_FOLDS.json', [])
            features['market_risk_forecasts'] = build_artifacts(out, market_inputs(data, features), spec, ph)
            with contextlib.redirect_stdout(io.StringIO()):
                evaluate_scores(data, features, scores, spec, out, 'test', ph)
            self.assertEqual(verify(out)['simulations'], 3)
            path = out/'simulations/all-cost15-candidate-har.json.gz'
            with gzip.open(path, 'rt') as stream:
                original = json.load(stream)
            self.assertGreater(original['metrics']['fill_count'], 0)
            for field, value, message in [('target_allocation', .01234, 'allocation differs'),
                                           ('signal_date', str(data['dates'][10]), 'previous session')]:
                changed = copy.deepcopy(original)
                changed['rebalance_log'][0][field] = value
                with gzip.open(path, 'wt') as stream:
                    json.dump(changed, stream)
                with self.assertRaisesRegex(ValueError, message):
                    verify(out)


if __name__ == '__main__':
    unittest.main()
