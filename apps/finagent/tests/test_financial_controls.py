"""Feature isolation, fixed seed comparisons and backward-compatible parameters."""
import copy
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from financial_history import FEATURE_NAMES
from quant_financial_controls import VIEWS, feature_view, independent_view, validate_study
from quant_tree_learning import TREE_PARAMS, tree_parameters


class FinancialControlTests(unittest.TestCase):
    def setUp(self):
        self.base = np.arange(4*5*72, dtype=np.float32).reshape(4, 5, 72)
        self.names = ['x'+str(i) for i in range(72)]
        self.eligible = np.ones((4, 5), bool)
        self.values = np.ones((4, 5, len(FEATURE_NAMES)), np.float32)
        self.values[..., 0] = [1, 2, 3, 4, 5]
        self.values[:, 2] = np.nan
        self.values[..., -2] = 6
        self.values[..., -1] = 200

    def test_every_view_keeps_original_columns_and_matches_independent_transform(self):
        expected = {'original': 72, 'profitability': 82, 'availability_context': 79, 'profitability_without_context': 78}
        before = self.base.copy()
        for view in VIEWS:
            matrix, names = feature_view(self.base, self.names, self.eligible, self.values, view)
            self.assertEqual(len(names), expected[view])
            np.testing.assert_array_equal(matrix[..., :72], before)
            np.testing.assert_array_equal(matrix, independent_view(self.base, self.names, self.eligible, self.values, view))
        np.testing.assert_array_equal(before, self.base)

    def test_numeric_changes_cannot_enter_context_only_control(self):
        changed = self.values.copy()
        changed[..., :3] *= -10
        for view, should_match in [('availability_context', True), ('profitability', False)]:
            a, _ = feature_view(self.base, self.names, self.eligible, self.values, view)
            b, _ = feature_view(self.base, self.names, self.eligible, changed, view)
            self.assertEqual(np.array_equal(a, b, equal_nan=True), should_match)

    def test_context_changes_cannot_enter_numeric_only_control(self):
        changed = self.values.copy()
        changed[..., -2:] *= 2
        for view, should_match in [('profitability_without_context', True), ('profitability', False)]:
            a, _ = feature_view(self.base, self.names, self.eligible, self.values, view)
            b, _ = feature_view(self.base, self.names, self.eligible, changed, view)
            self.assertEqual(np.array_equal(a, b, equal_nan=True), should_match)

    def test_missing_numeric_observations_keep_masks_and_no_backward_fill(self):
        a, names = feature_view(self.base, self.names, self.eligible, self.values, 'profitability_without_context')
        self.assertTrue(np.isnan(a[:, 2, 72::2]).all())
        self.assertTrue((a[:, 2, 73::2] == 0).all())
        changed = self.values.copy()
        changed[2:] *= -3
        b, _ = feature_view(self.base, self.names, self.eligible, changed, 'profitability_without_context')
        np.testing.assert_array_equal(a[:2], b[:2])

    def test_seeds_are_validated_isolated_and_default_parameters_unchanged(self):
        original = copy.deepcopy(TREE_PARAMS)
        self.assertEqual(tree_parameters(), original)
        self.assertEqual(tree_parameters(261016), original)
        alternate = tree_parameters(261017)
        self.assertEqual({k: v for k, v in alternate.items() if k != 'seed'}, {k: v for k, v in original.items() if k != 'seed'})
        alternate['num_leaves'] = 999
        self.assertEqual(TREE_PARAMS, original)
        for value in (-1, 2**31, True, 1.5, '261017'):
            with self.assertRaises(ValueError):
                tree_parameters(value)

    def test_study_rejects_seed_selection_and_view_or_cadence_drift(self):
        path = Path(__file__).resolve().parents[3]/'config/research/financial_controls.json'
        study = json.loads(path.read_text())
        validate_study(study)
        for change in [lambda s: s.update(seeds=[261017]),
                       lambda s: s['feature_views'].update(annual_financial_context='profitability'),
                       lambda s: s['variants']['annual_original_seed261017'].update(tree_seed=261018),
                       lambda s: s['variants']['annual_original_seed261017'].update(cadence='monthly')]:
            bad = copy.deepcopy(study)
            change(bad)
            with self.assertRaises(ValueError):
                validate_study(bad)


if __name__ == '__main__':
    unittest.main()
