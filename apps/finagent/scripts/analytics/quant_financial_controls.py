"""Separate financial values, observation context and training randomness."""
from __future__ import annotations

import numpy as np

from horizon_signals import validate_study as validate_horizons
from quant_financial_learning import GROUPS, MODE, append_financial_features
from quant_tree_learning import TREE_PARAMS, tree_parameters


VIEWS = ('original', 'profitability', 'availability_context', 'profitability_without_context')
SEEDS = (261016, 261017, 261018)
VARIANTS = {
    'annual_financial_context': ('availability_context', 261016),
    'annual_profitability_no_context': ('profitability_without_context', 261016),
    'annual_original_seed261017': ('original', 261017),
    'annual_profitability_seed261017': ('profitability', 261017),
    'annual_original_seed261018': ('original', 261018),
    'annual_profitability_seed261018': ('profitability', 261018),
}
REFERENCES = {'annual_tree_blend': ('original', 261016), 'annual_profitability': ('profitability', 261016)}


def validate_study(study):
    validate_horizons(study)
    expected_views = {name: view for name, (view, _) in {**REFERENCES, **VARIANTS}.items()}
    expected_seeds = {name: seed for name, (_, seed) in {**REFERENCES, **VARIANTS}.items()}
    if (study['feature_views'] != expected_views or study['tree_seeds_by_model'] != expected_seeds
            or set(study['variants']) != set(VARIANTS)
            or study['prediction_reference_map'] != {name: name for name in REFERENCES}
            or study['label_horizon'] != 20 or study['validation_sessions'] != 63
            or study['score_blends'] or study['financial_clock'] != MODE or study['baseline_features'] != 72
            or study['context_scales'] != {'reported_period_months': 12, 'report_age_days': 550}
            or study['seeds'] != list(SEEDS) or TREE_PARAMS['seed'] != SEEDS[0]):
        raise ValueError('Financial controls differ from the predeclared views and seed comparisons')
    for name, (_, seed) in VARIANTS.items():
        if study['variants'][name] != {'family': 'tree_blend', 'cadence': 'annual', 'half_life_sessions': None, 'tree_seed': seed}:
            raise ValueError('Financial controls cannot change the learner, cadence or declared seed')
        tree_parameters(seed)


def feature_view(base, names, eligible, values, view):
    """Slice the original profit feature definition without altering any values."""
    if view not in VIEWS:
        raise ValueError('Unknown financial control view')
    if view == 'original':
        return base, list(names)
    matrix, labels = append_financial_features(base, names, eligible, values, GROUPS['profitability'])
    count = len(names)
    if view == 'profitability':
        return matrix, labels
    extra = [1, 3, 5, 6, 7, 8, 9] if view == 'availability_context' else list(range(6))
    columns = [*range(count), *(count+i for i in extra)]
    return matrix[..., columns], [labels[i] for i in columns]


def independent_view(base, names, eligible, values, view):
    """Rebuild selected extras independently, with the existing SciPy verifier."""
    from verify_financial_ablation import reference_extras
    if view not in VIEWS:
        raise ValueError('Unknown financial control view')
    if view == 'original':
        return base
    extras = reference_extras(values, eligible, GROUPS['profitability'])
    if view == 'availability_context':
        extras = extras[..., [1, 3, 5, 6, 7, 8, 9]]
    elif view == 'profitability_without_context':
        extras = extras[..., :6]
    return np.concatenate([base, extras.astype(base.dtype)], axis=-1)
