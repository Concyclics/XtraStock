"""Financial ablation invariants, missingness, time isolation and replay math."""
import copy
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from financial_history import FEATURE_NAMES
from quant_financial_learning import GROUPS, append_financial_features, validate_study
from quant_learning import predict
from verify_financial_ablation import reference_extras, replay_ridge


class FinancialAblationTests(unittest.TestCase):
    def setUp(self):
        self.base = np.arange(4*5*2, dtype=np.float32).reshape(4, 5, 2)
        self.base[0, 0, 0] = np.nan
        self.eligible = np.ones((4, 5), bool)
        self.eligible[:, -1] = False
        self.values = np.ones((4, 5, len(FEATURE_NAMES)), np.float32)
        self.values[..., 0] = [1, 3, 3, np.nan, 99]
        self.values[..., -2] = 6
        self.values[..., -1] = 275
        self.values[:, 3] = np.nan

    def test_baseline_is_unchanged_and_missing_financials_do_not_remove_a_stock(self):
        before, eligible = self.base.copy(), self.eligible.copy()
        matrix, names = append_financial_features(self.base, ['a', 'b'], self.eligible, self.values, GROUPS['profitability'])
        np.testing.assert_array_equal(self.base, before)
        np.testing.assert_array_equal(matrix[..., :2], before)
        np.testing.assert_array_equal(self.eligible, eligible)
        np.testing.assert_allclose(matrix[0, :3, 2], [1/3, 5/6, 5/6])
        self.assertTrue(np.isnan(matrix[0, 3, 2]))
        self.assertEqual(matrix[0, 3, 3], 0)
        self.assertTrue(np.isnan(matrix[0, 4, 2]))
        self.assertEqual(matrix[0, 4, 3], 0)
        self.assertEqual(names[:2], ['a', 'b'])
        self.assertEqual(matrix.shape[-1], 12)

    def test_future_values_do_not_change_any_earlier_feature(self):
        changed = self.values.copy()
        changed[2:] *= -100
        old, _ = append_financial_features(self.base, ['a', 'b'], self.eligible, self.values, GROUPS['combined'])
        new, _ = append_financial_features(self.base, ['a', 'b'], self.eligible, changed, GROUPS['combined'])
        np.testing.assert_array_equal(old[:2], new[:2])

    def test_independent_scipy_reference_matches_every_group_and_context_is_not_annualized(self):
        for group in GROUPS.values():
            matrix, _ = append_financial_features(self.base, ['a', 'b'], self.eligible, self.values, group)
            reference = reference_extras(self.values, self.eligible, group).astype(matrix.dtype)
            np.testing.assert_array_equal(matrix[..., 2:], reference)
            self.assertEqual(matrix[0, 0, -4], .5)
            self.assertEqual(matrix[0, 0, -2], .5)

    def test_a_wholly_missing_day_keeps_nan_ranks_and_zero_masks(self):
        self.values[0] = np.nan
        matrix, _ = append_financial_features(self.base, ['a', 'b'], self.eligible, self.values, GROUPS['combined'])
        self.assertTrue(np.isnan(matrix[0, :, 2::2]).all())
        self.assertTrue((matrix[0, :, 3::2] == 0).all())

    def test_configuration_cannot_silently_change_training_or_add_unverified_fields(self):
        study = json.loads((Path(__file__).resolve().parents[3]/'config/research/financial_ablation.json').read_text())
        validate_study(study)
        for mutate in [lambda s: s.update(label_horizon=5),
                       lambda s: s['variants']['annual_growth'].update(cadence='monthly'),
                       lambda s: s['financial_groups']['annual_combined'].append('cash_to_book'),
                       lambda s: s.update(financial_clock='system_observed'),
                       lambda s: s['context_scales'].update(reported_period_months=1)]:
            bad = copy.deepcopy(study)
            mutate(bad)
            with self.assertRaises(ValueError):
                validate_study(bad)

    def test_dimension_and_duplicate_name_errors_are_explicit(self):
        with self.assertRaises(ValueError):
            append_financial_features(self.base, ['a', 'a'], self.eligible, self.values, GROUPS['growth'])
        with self.assertRaises(ValueError):
            append_financial_features(self.base, ['a', 'b'], self.eligible, self.values[:, :-1], GROUPS['growth'])

    def test_saved_ridge_replay_preserves_training_imputation_and_clipping(self):
        model = {'median': [1, 2], 'mean': [2, 4], 'scale': [2, 3], 'coefficient': [.5, .1, -.2]}
        matrix = np.array([[np.nan, 1], [1e9, -1e9], [3, 4.]])
        expected = predict({k: np.array(v) for k, v in model.items()}, matrix)
        np.testing.assert_array_equal(replay_ridge(model, matrix), expected)


if __name__ == '__main__':
    unittest.main()
