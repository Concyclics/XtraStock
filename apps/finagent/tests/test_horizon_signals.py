"""Horizon provenance, genuine purged fits and independently audited blends."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from horizon_signals import blend_scores, validate_study
from quant_adaptation import prediction_periods, rolling_splits, walk_forward_adaptation
from strategy_dataset import checksum, dump
from verify_adaptation_cycle import audit_predictions, predictive_diagnostics


class HorizonTests(unittest.TestCase):
    def test_fixed_blend_is_scale_invariant_and_never_fills_missing_models(self):
        scores = {'short': np.array([[1., 3., 2., np.nan]]), 'long': np.array([[4., 2., 1., 3.]])}
        eligible = np.ones((1, 4), bool)
        definition = {'blend': {'components': ['short', 'long'], 'weights': [.5, .5]}}
        expected = blend_scores(scores, eligible, definition)['blend']
        other = blend_scores({'short': scores['short']*1000+100, 'long': scores['long']*.001}, eligible, definition)
        np.testing.assert_array_equal(expected, other['blend'])
        self.assertTrue(np.isnan(expected[0, -1]))
        for weights in ([.5, .4], [1., 0.], [np.nan, .5]):
            with self.assertRaisesRegex(ValueError, 'Invalid fixed'):
                blend_scores(scores, eligible, {'blend': {'components': ['short', 'long'], 'weights': weights}})
        with self.assertRaisesRegex(ValueError, 'Missing or mismatched'):
            blend_scores({'short': scores['short']}, eligible, definition)

    def test_invalid_horizon_provenance_is_rejected_before_training(self):
        path = Path(__file__).resolve().parents[3] / 'config/research/horizons.json'
        study = json.loads(path.read_text())
        validate_study(study)
        for name, values in [('annual_tree_h5', [20]), ('multihorizon_tree', [5]), ('multihorizon_ridge', [20, 5])]:
            changed = copy.deepcopy(study)
            changed['model_horizons'][name] = values
            with self.assertRaises(ValueError):
                validate_study(changed)
        changed = copy.deepcopy(study)
        changed['score_blends']['multihorizon_tree']['components'][0] = 'multihorizon_ridge'
        with self.assertRaisesRegex(ValueError, 'base predictions'):
            validate_study(changed)

    def test_diagnostics_belong_to_next_open_year_and_exclude_unexecutable_last_signal(self):
        dates = np.array(['2023-12-28', '2023-12-29', '2024-01-02', '2024-01-03'])
        data = {'dates': dates, 'symbols': np.arange(80),
                'cohort': np.array(['discovery']*40 + ['cross_section_reserve']*40)}
        values = np.tile(np.arange(80, dtype=float), (4, 1))
        outcome = values.copy()
        outcome[1] *= -1
        outcome[2] = np.nan
        rows = predictive_diagnostics(data, {'eligible': np.ones((4, 80), bool)},
            {'candidate': values}, outcome, [2023, 2024], 5, next_open=True)
        table = {(r['cohort'], r['year']): r for r in rows}
        self.assertAlmostEqual(table['all', 2023]['mean_daily_rank_ic'], 1)
        self.assertAlmostEqual(table['all', 2024]['mean_daily_rank_ic'], -1)
        self.assertEqual(table['all', 2024]['eligible_predictions'], 160)
        self.assertEqual(table['all', 'all']['labeled_days'], 2)
        self.assertEqual(table['all', 'all']['date_ownership'], 'next_open')
        self.assertEqual(table['all', 'all']['horizon'], 5)

    def test_short_labels_mature_fifteen_sessions_earlier_with_same_primary_boundary(self):
        dates = pd.bdate_range('2020-01-01', '2023-02-28').strftime('%Y-%m-%d').to_numpy()
        period = prediction_periods(dates, [2023], 'annual')[0]
        mask = np.ones((len(dates), 2), bool)
        short = rolling_splits(dates, mask, period, '2021-10-15', horizon=5)
        long = rolling_splits(dates, mask, period, '2021-10-15', horizon=20)
        self.assertEqual(short[0].any(axis=1).sum() - long[0].any(axis=1).sum(), 15)
        for horizon, (train, inner, valid) in [(5, short), (20, long)]:
            td, ii, vi = [np.flatnonzero(m.any(axis=1)) for m in (train, inner, valid)]
            self.assertGreaterEqual(dates[td[0]], '2021-10-15')
            self.assertLess(td[-1]+horizon, period['first_open_index'])
            self.assertLess(ii[-1]+horizon, vi[0])
            self.assertEqual(len(vi), 63)

    def test_real_annual_fit_is_causal_resumable_and_blend_tampering_is_detected(self):
        rng = np.random.default_rng(1610)
        dates = pd.bdate_range('2021-10-15', '2023-02-28').strftime('%Y-%m-%d').to_numpy()
        close = 10*np.exp(np.cumsum(rng.normal(0, .01, (len(dates), 8)), axis=0))
        data = {'dates': dates, 'close': close, 'open': close*.999, 'volume': np.full_like(close, 1e6),
                'total_return_factor': np.ones_like(close), 'adjustment_quality': np.ones_like(close),
                'primary_since': '2021-10-15', 'cohort': np.array(['discovery']*6+['cross_section_reserve']*2)}
        features = {'valid': np.ones_like(close, bool), 'eligible': np.ones_like(close, bool)}
        matrix = rng.normal(size=(*close.shape, 3))
        variants = {'short': {'family': 'ridge_extended', 'cadence': 'annual', 'half_life_sessions': None}}
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)
            original, folds = walk_forward_adaptation(data, features, matrix, ['a', 'b', 'c'], [2023],
                variants, out/'first', 'p', horizon=5)
            resumed, _ = walk_forward_adaptation(data, features, matrix, ['a', 'b', 'c'], [2023],
                variants, out/'first', 'p', horizon=5)
            np.testing.assert_array_equal(original['short'], resumed['short'])
            changed = {k: v.copy() if isinstance(v, np.ndarray) else v for k, v in data.items()}
            changed['open'][dates >= '2023-01-16'] *= np.arange(1, 9)[None, :]
            changed['close'][dates >= '2023-01-16'] *= np.arange(1, 9)[None, :]
            altered, _ = walk_forward_adaptation(changed, features, matrix, ['a', 'b', 'c'], [2023],
                variants, out/'other', 'q', horizon=5)
            np.testing.assert_array_equal(original['short'], altered['short'])
            self.assertEqual(len(folds), 1)

            target, reference = out/'first', out/'reference'
            reference.mkdir()
            np.savez_compressed(reference/'PREDICTIONS.npz', prior=original['short']*-2)
            dump(reference/'PROTOCOL.json', {'horizon': 20, 'synthetic_reference': True})
            study = {'models': ['short', 'long', 'blend'], 'variants': variants, 'training_years': 3,
                'validation_sessions': 63, 'label_horizon': 5, 'prediction_reference_map': {'long': 'prior'},
                'model_horizons': {'short': [5], 'long': [20], 'blend': [5, 20]},
                'score_blends': {'blend': {'components': ['short', 'long'], 'weights': [.5, .5]}}}
            dump(target/'STUDY_CONFIG.json', study)
            spec = {**study, 'training_variants': variants, 'code': {}, 'primary_since': data['primary_since'],
                'horizon': 5, 'test_years': [2023], 'study_config_sha256': checksum(target/'STUDY_CONFIG.json'),
                'annual_reference_predictions_sha256': checksum(reference/'PREDICTIONS.npz'),
                'annual_reference_protocol_sha256': checksum(reference/'PROTOCOL.json')}
            dump(target/'PROTOCOL.json', spec)
            digest = checksum(target/'PROTOCOL.json')
            receipt_path = target/'folds/short/2023-01-01.json'
            receipt = json.loads(receipt_path.read_text())
            receipt['protocol_sha256'] = digest
            dump(receipt_path, receipt)
            outputs = {**original, 'long': original['short']*-2}
            outputs.update(blend_scores(outputs, features['eligible'], study['score_blends']))
            np.savez_compressed(target/'PREDICTIONS.npz', **outputs)
            dump(target/'PREDICTION_RECEIPT.json', {'protocol_sha256': digest, 'sha256': checksum(target/'PREDICTIONS.npz')})
            dump(target/'COMPLETE.json', {'protocol_sha256': digest, 'status': 'adaptation_predictions_complete', 'completed_training_folds': 1})
            _, _, report = audit_predictions(target, data, features, reference)
            self.assertEqual(report['folds'], 1)
            outputs['blend'][folds[0]['signal_begin'], 0] += .1
            np.savez_compressed(target/'PREDICTIONS.npz', **outputs)
            dump(target/'PREDICTION_RECEIPT.json', {'protocol_sha256': digest, 'sha256': checksum(target/'PREDICTIONS.npz')})
            with self.assertRaisesRegex(ValueError, 'fixed contemporaneous'):
                audit_predictions(target, data, features, reference)


if __name__ == '__main__':
    unittest.main()
