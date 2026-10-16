"""Lossless controls, equal-seed aggregation and independent reconstruction."""
import copy
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from seed_ensemble import GROUPS, REFERENCES, combine, validate_reference, validate_study, verify_combinations


class SeedEnsembleTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[3]
        self.study = json.loads((root/'config/research/seed_ensemble.json').read_text())
        self.reference_study = json.loads((root/'config/research/financial_controls.json').read_text())
        arrays = [np.array([[1., 1., 4., 2.], [1., 4., 3., 2.]]),
                  np.array([[4., 3., 2., 1.], [2., 1., 4., 3.]]),
                  np.array([[2., 4., 1., 3.], [3., 2., 1., 4.]])]
        self.scores = {name: arrays[i].copy() for members in GROUPS.values() for i, name in enumerate(members)}
        self.eligible = np.ones((2, 4), bool)

    def test_all_seeds_have_equal_influence_with_average_ties(self):
        result = combine(self.scores, self.eligible, self.study)
        # First component tied ranks: 1.5/4, 1.5/4, 4/4, 3/4.
        expected = (np.array([.375, .375, 1., .75])+[1., .75, .5, .25]+np.array([.5, 1., .25, .75]))/3
        for name in GROUPS:
            np.testing.assert_allclose(result[name][0], expected, rtol=0, atol=1e-15)
        checks = verify_combinations({**self.scores, **result}, self.eligible, self.study)
        self.assertTrue(all(r['independent_aggregate_match'] and r['predictions']==8 for r in checks))

    def test_monotonic_scale_changes_and_future_rows_do_not_change_prior_predictions(self):
        original = combine(self.scores, self.eligible, self.study)
        altered = {name: value*1000+i for i, (name, value) in enumerate(self.scores.items())}
        transformed = combine(altered, self.eligible, self.study)
        for name in GROUPS:
            np.testing.assert_array_equal(original[name], transformed[name])
        for value in altered.values():
            value[1] *= -1
        future = combine(altered, self.eligible, self.study)
        for name in GROUPS:
            np.testing.assert_array_equal(original[name][0], future[name][0])

    def test_missing_seed_coverage_and_infinite_scores_are_rejected(self):
        changed = copy.deepcopy(self.scores)
        changed[next(iter(REFERENCES))][0, 0] = np.nan
        with self.assertRaisesRegex(ValueError, 'coverage differs'):
            combine(changed, self.eligible, self.study)
        changed[next(iter(REFERENCES))][0, 0] = np.inf
        with self.assertRaisesRegex(ValueError, 'infinity'):
            combine(changed, self.eligible, self.study)
        changed.pop(next(iter(REFERENCES)))
        with self.assertRaisesRegex(ValueError, 'six'):
            combine(changed, self.eligible, self.study)

    def test_warmup_and_ineligible_security_remain_missing(self):
        for value in self.scores.values():
            value[0] = np.nan
        self.eligible[1, -1] = False
        result = combine(self.scores, self.eligible, self.study)
        for value in result.values():
            self.assertTrue(np.isnan(value[0]).all())
            self.assertTrue(np.isnan(value[1, -1]))
        checks = verify_combinations({**self.scores, **result}, self.eligible, self.study)
        self.assertTrue(all(r['predictions']==3 for r in checks))

    def test_seed_replacement_unequal_weights_and_new_fit_are_rejected(self):
        validate_study(self.study)
        for field, value in [('seeds', [261016, 261017, 9]), ('label_horizon', 5),
                             ('aggregation', 'return_fitted_weights'), ('validation_sessions', 42)]:
            changed = copy.deepcopy(self.study)
            changed[field] = value
            with self.assertRaises(ValueError):
                validate_study(changed)
        for weights in ([.5, .25, .25], [1/3, 1/3, .3]):
            changed = copy.deepcopy(self.study)
            changed['score_blends']['annual_original_seed_mean']['weights'] = weights
            with self.assertRaises(ValueError):
                validate_study(changed)
        changed = copy.deepcopy(self.study)
        changed['variants']['new_fit'] = {'family': 'tree_blend'}
        with self.assertRaises(ValueError):
            validate_study(changed)

    def test_reference_seed_family_and_account_contract_cannot_drift(self):
        reference = {**self.reference_study, 'primary_since': '2021-10-15', 'test_years': [2023, 2024, 2025, 2026],
                     'source': {}, 'forecast_sha256': 'fixture'}
        validate_reference(self.study, reference, self.reference_study)
        for field, value in [('tree_seeds_by_model', 9), ('feature_views', 'availability_context')]:
            changed = copy.deepcopy(reference)
            changed[field]['annual_profitability_seed261017'] = value
            with self.assertRaisesRegex(ValueError, 'Source seed'):
                validate_reference(self.study, changed, self.reference_study)
        altered = copy.deepcopy(self.reference_study)
        altered['allocations']['k100_equal']['size'] = 50
        with self.assertRaisesRegex(ValueError, 'account or training'):
            validate_reference(self.study, reference, altered)

    def test_independent_replay_detects_modified_aggregate_and_does_not_mutate_sources(self):
        before = {name: value.copy() for name, value in self.scores.items()}
        result = combine(self.scores, self.eligible, self.study)
        for name in REFERENCES:
            np.testing.assert_array_equal(before[name], self.scores[name])
        result['annual_original_seed_mean'][0, 0] += .01
        with self.assertRaisesRegex(ValueError, 'independent'):
            verify_combinations({**self.scores, **result}, self.eligible, self.study)


if __name__ == '__main__':
    unittest.main()
