"""Real fits, temporal ownership and resume integrity for periodic research."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from quant_learning import observation_weights, fit_ridge, purged_training_dates
from quant_tree_learning import day_weights
from quant_adaptation import prediction_periods, rolling_splits, walk_forward_adaptation
from verify_adaptation_cycle import audit_predictions, predictive_diagnostics
from strategy_dataset import checksum, dump


class AdaptationTests(unittest.TestCase):
    def test_uniform_weights_preserve_existing_tree_contract(self):
        idx = np.array([0, 0, 1, 2, 2, 2])
        np.testing.assert_array_equal(observation_weights(idx), day_weights(idx))
        sums = [observation_weights(idx)[idx == day].sum() for day in range(3)]
        np.testing.assert_allclose(sums, [2, 2, 2])

    def test_recency_means_half_weight_per_half_life_not_per_row(self):
        idx = np.array([0, 0, 252, 252, 252, 504])
        weight = observation_weights(idx, 252)
        sums = [weight[idx == day].sum() for day in (0, 252, 504)]
        np.testing.assert_allclose(np.array(sums) / sums[-1], [.25, .5, 1])
        with self.assertRaises(ValueError):
            observation_weights(idx, 0)

    def test_weighted_ridge_intercept_matches_weighted_target(self):
        idx = np.repeat(np.arange(126), 10)
        target = np.where(idx < 63, -.2, .2)
        x = np.zeros((len(idx), 2))
        weights = observation_weights(idx, 63)
        uniform = fit_ridge(x, target, idx)
        recent = fit_ridge(x, target, idx, half_life_sessions=63)
        self.assertAlmostEqual(uniform['coefficient'][0], 0)
        self.assertAlmostEqual(recent['coefficient'][0], np.average(target, weights=weights))
        self.assertGreater(recent['coefficient'][0], 0)

    def test_rank_diagnostics_use_only_mature_labels_and_preserve_cohorts(self):
        dates = np.array(['2023-12-29', '2024-01-02', '2024-01-03'])
        data = {'dates': dates, 'symbols': np.arange(80),
                'cohort': np.array(['discovery'] * 40 + ['cross_section_reserve'] * 40)}
        values = np.tile(np.arange(80, dtype=float), (3, 1))
        outcomes = values.copy()
        outcomes[1, 40:] *= -1
        outcomes[2] = np.nan  # Final observations have no matured outcome.
        rows = predictive_diagnostics(data, {'eligible': np.ones((3, 80), bool)},
                                      {'score': values}, outcomes, [2023, 2024])
        table = {(r['cohort'], r['year']): r for r in rows}
        discovery = table['discovery', 2024]
        reserve = table['cross_section_reserve', 2024]
        self.assertAlmostEqual(discovery['mean_daily_rank_ic'], 1)
        self.assertAlmostEqual(reserve['mean_daily_rank_ic'], -1)
        self.assertEqual(reserve['positive_daily_rank_ic_fraction'], 0)
        self.assertEqual(discovery['eligible_predictions'], 80)
        self.assertEqual(discovery['labeled_predictions'], 40)
        self.assertEqual(discovery['labeled_days'], 1)
        self.assertEqual(table['discovery', 'all']['labeled_days'], 2)
        self.assertIn('not executed profit', discovery['label_contract'])

    def test_next_open_periods_have_no_overlap_or_gap(self):
        dates = pd.bdate_range('2020-01-01', '2026-10-15').strftime('%Y-%m-%d').to_numpy()
        for cadence, expected in [('quarterly', 16), ('monthly', 46)]:
            periods = prediction_periods(dates, [2023, 2024, 2025, 2026], cadence)
            self.assertEqual(len(periods), expected)
            ownership = np.zeros(len(dates), int)
            for period in periods:
                begin, end = period['signal_begin'], period['signal_end_exclusive']
                ownership[begin:end] += 1
                self.assertEqual(begin + 1, period['first_open_index'])
            self.assertTrue((ownership[periods[0]['signal_begin']:] == 1).all())
            self.assertTrue((ownership[:periods[0]['signal_begin']] == 0).all())
        last = prediction_periods(dates, [2023], 'monthly')[-1]
        self.assertLess(dates[last['signal_end_exclusive'] - 1], '2024-01-01')
        with self.assertRaises(ValueError):
            prediction_periods(dates, [2023, 2025], 'quarterly')

    def test_january_split_matches_annual_and_later_splits_move_window(self):
        dates = pd.bdate_range('2020-01-01', '2026-10-15').strftime('%Y-%m-%d').to_numpy()
        periods = prediction_periods(dates, [2023, 2024, 2025, 2026], 'quarterly')
        mask = np.ones((len(dates), 4), bool)
        train, _, _ = rolling_splits(dates, mask, periods[0], '2021-10-15')
        old, first = purged_training_dates(dates, 2023, 20, '2021-10-15')
        np.testing.assert_array_equal(train, np.broadcast_to(old[:, None], train.shape))
        self.assertEqual(first, periods[0]['first_open_index'])
        for period in periods:
            train, inner, valid = rolling_splits(dates, mask, period, '2021-10-15')
            td = np.flatnonzero(train.any(axis=1)); ii = np.flatnonzero(inner.any(axis=1)); vi = np.flatnonzero(valid.any(axis=1))
            lower = max('2021-10-15', (pd.Timestamp(period['period_start']) - pd.DateOffset(years=3)).strftime('%Y-%m-%d'))
            self.assertGreaterEqual(dates[td[0]], lower)
            self.assertLess(td[-1] + 20, period['first_open_index'])
            self.assertLess(ii[-1] + 20, vi[0])
            self.assertEqual(len(vi), 63)

    def test_real_periodic_fit_cannot_see_future_and_tampered_resume_fails(self):
        rng = np.random.default_rng(1516)
        dates = pd.bdate_range('2021-10-15', '2023-02-28').strftime('%Y-%m-%d').to_numpy()
        close = 10 * np.exp(np.cumsum(rng.normal(0, .01, (len(dates), 8)), axis=0))
        data = {'dates': dates, 'close': close, 'open': close * .999,
                'volume': np.full_like(close, 1e6), 'total_return_factor': np.ones_like(close),
                'adjustment_quality': np.ones_like(close), 'primary_since': '2021-10-15',
                'cohort': np.array(['discovery'] * 6 + ['cross_section_reserve'] * 2)}
        features = {'valid': np.ones_like(close, bool), 'eligible': np.ones_like(close, bool)}
        matrix = rng.normal(size=(*close.shape, 3))
        variants = {'monthly': {'family': 'ridge_extended', 'cadence': 'monthly', 'half_life_sessions': 252}}
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            first, folds = walk_forward_adaptation(data, features, matrix, ['a', 'b', 'c'], [2023], variants, out / 'first', 'p')
            resumed, _ = walk_forward_adaptation(data, features, matrix, ['a', 'b', 'c'], [2023], variants, out / 'first', 'p')
            np.testing.assert_array_equal(first['monthly'], resumed['monthly'])
            changed = {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in data.items()}
            changed['close'][dates >= '2023-01-16'] *= np.arange(1, 9)[None, :]
            changed['open'][dates >= '2023-01-16'] *= np.arange(1, 9)[None, :]
            other, _ = walk_forward_adaptation(changed, features, matrix, ['a', 'b', 'c'], [2023], variants, out / 'other', 'q')
            section = (dates < '2023-01-16')
            np.testing.assert_array_equal(first['monthly'][section], other['monthly'][section])
            self.assertEqual(len(folds), 2)
            path = next((out / 'first/folds/monthly').glob('*.npz'))
            path.write_bytes(path.read_bytes() + b'tampered')
            with self.assertRaisesRegex(ValueError, 'provenance'):
                walk_forward_adaptation(data, features, matrix, ['a', 'b', 'c'], [2023], variants, out / 'first', 'p')

            # Audit a real tiny fit, then prove that the independent verifier
            # detects a claim that a future return was used during training.
            target = out / 'other'
            reference = out / 'reference'
            reference.mkdir()
            np.savez_compressed(reference / 'PREDICTIONS.npz', tree_blend=other['monthly'], ridge_extended=other['monthly'])
            dump(reference / 'PROTOCOL.json', {'test_reference': True})
            outputs = {**other, 'annual_tree_blend': other['monthly'], 'annual_ridge_extended': other['monthly']}
            study = {'models': list(outputs), 'variants': variants, 'training_years': 3, 'validation_sessions': 63}
            dump(target / 'STUDY_CONFIG.json', study)
            spec = {**study, 'training_variants': variants, 'code': {}, 'primary_since': data['primary_since'],
                    'horizon': 20, 'test_years': [2023], 'study_config_sha256': checksum(target / 'STUDY_CONFIG.json'),
                    'annual_reference_predictions_sha256': checksum(reference / 'PREDICTIONS.npz'),
                    'annual_reference_protocol_sha256': checksum(reference / 'PROTOCOL.json')}
            dump(target / 'PROTOCOL.json', spec)
            digest = checksum(target / 'PROTOCOL.json')
            for receipt_path in (target / 'folds/monthly').glob('*.json'):
                receipt = json.loads(receipt_path.read_text())
                receipt['protocol_sha256'] = digest
                dump(receipt_path, receipt)
            np.savez_compressed(target / 'PREDICTIONS.npz', **outputs)
            dump(target / 'PREDICTION_RECEIPT.json', {'protocol_sha256': digest, 'sha256': checksum(target / 'PREDICTIONS.npz')})
            dump(target / 'COMPLETE.json', {'protocol_sha256': digest, 'status': 'adaptation_predictions_complete', 'completed_training_folds': 2})
            _, _, report = audit_predictions(target, changed, features, reference)
            self.assertEqual(report['folds'], 2)
            records = json.loads((target / 'TRAINING_FOLDS.json').read_text())
            records[0]['train_label_last'] = '2023-01-16'
            dump(target / 'TRAINING_FOLDS.json', records)
            with self.assertRaisesRegex(ValueError, 'training history differs'):
                audit_predictions(target, changed, features, reference)


if __name__ == '__main__':
    unittest.main()
