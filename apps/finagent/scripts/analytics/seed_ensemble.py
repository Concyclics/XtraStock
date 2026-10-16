"""Fixed, symmetric seed aggregation without fitted weights or missing-seed fallback."""
from __future__ import annotations

import numpy as np

from horizon_signals import blend_scores, validate_study as validate_horizon_study

SEEDS = [261016, 261017, 261018]
GROUPS = {
    'annual_original_seed_mean': ['annual_tree_blend', 'annual_original_seed261017', 'annual_original_seed261018'],
    'annual_profitability_seed_mean': ['annual_profitability', 'annual_profitability_seed261017', 'annual_profitability_seed261018'],
}
REFERENCES = {name: name for group in GROUPS.values() for name in group}


def validate_study(study):
    validate_horizon_study(study)
    expected = {name: {'components': group, 'weights': [1/3]*3} for name, group in GROUPS.items()}
    if (study['variants'] or study['prediction_reference_map'] != REFERENCES
            or study['score_blends'] != expected or study['seeds'] != SEEDS
            or set(study['models']) != set(REFERENCES) | set(GROUPS)
            or study['aggregation'] != 'equal_weight_contemporaneous_ranks'
            or study['label_horizon'] != 20 or study['validation_sessions'] != 63
            or any(h != [20] for h in study['model_horizons'].values())
            or study['diagnostic_horizons'] != [20] or study['diagnostic_next_open'] is not True):
        raise ValueError('Seed study must retain all fixed seeds, equal weights and unchanged label contracts')


def validate_reference(study, reference, reference_study):
    validate_study(study)
    for key in ('primary_since', 'test_years', 'source', 'forecast_sha256'):
        if key not in reference:
            raise ValueError('Missing source prediction provenance')
    for key in ('allocations', 'cohorts', 'cost_bps_per_side', 'capital_levels', 'training_years', 'validation_sessions'):
        if study[key] != reference_study[key]:
            raise ValueError('Seed comparison changed the reference account or training contract: '+key)
    for ensemble, components in GROUPS.items():
        view = 'profitability' if 'profitability' in ensemble else 'original'
        for name, seed in zip(components, SEEDS):
            if (reference['tree_seeds_by_model'].get(name) != seed
                    or reference['feature_views'].get(name) != view
                    or reference['model_horizons'].get(name) != [20]):
                raise ValueError('Source seed, feature family or label horizon differs')


def checked_components(scores, eligible):
    if set(scores) != set(REFERENCES):
        raise ValueError('All six predeclared source predictions are required')
    masks = []
    for value in scores.values():
        if value.shape != eligible.shape or np.isinf(value).any():
            raise ValueError('Invalid source score dimensions or infinity')
        masks.append(eligible & np.isfinite(value))
    if any(not np.array_equal(mask, masks[0]) for mask in masks[1:]):
        raise ValueError('Source prediction coverage differs; no missing-seed fallback')
    return masks[0]


def combine(scores, eligible, study):
    validate_study(study)
    checked_components(scores, eligible)
    return blend_scores(scores, eligible, study['score_blends'])


def verify_combinations(scores, eligible, study):
    """Use SciPy rankdata and an explicit arithmetic mean as an independent route."""
    from scipy.stats import rankdata

    validate_study(study)
    source = {name: scores[name] for name in REFERENCES}
    mask = checked_components(source, eligible)
    counts = mask.sum(axis=1, keepdims=True)
    checks = []
    for name, components in GROUPS.items():
        ranked = []
        for component in components:
            values = rankdata(np.where(mask, source[component], np.nan), axis=1,
                              method='average', nan_policy='omit')
            ranked.append(np.divide(values, counts, out=np.full_like(values, np.nan), where=counts>0))
        expected = np.stack(ranked).mean(axis=0)
        if (not np.array_equal(np.isfinite(scores[name]), mask)
                or not np.allclose(expected, scores[name], atol=1e-12, rtol=0, equal_nan=True)):
            raise ValueError('Seed aggregate differs from independent equal-rank reconstruction')
        checks.append({'model': name, 'year': '2023-2026', 'seeds': 3, 'predictions': int(mask.sum()),
                       'maximum_score_difference': float(np.nanmax(np.abs(expected-scores[name]))),
                       'all_seed_scores_preserved': True, 'independent_aggregate_match': True})
    return checks
