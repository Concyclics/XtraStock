"""Fixed multi-horizon rank combinations with explicit forecast provenance."""
from __future__ import annotations

import numpy as np

from strategy_research import rank


def blend_scores(scores, eligible, definitions):
    """No fitted outcome weights, score-scale mixing or missing-model fallback."""
    result = {}
    for name, definition in definitions.items():
        components = definition['components']
        weights = np.asarray(definition['weights'], float)
        if (name in scores or len(components) != len(weights) or len(components) < 2
                or len(set(components)) != len(components) or np.any(~np.isfinite(weights))
                or np.any(weights <= 0) or not np.isclose(weights.sum(), 1., atol=1e-12, rtol=0)):
            raise ValueError('Invalid fixed horizon blend')
        if any(key not in scores or scores[key].shape != eligible.shape for key in components):
            raise ValueError('Missing or mismatched horizon component')
        common = eligible & np.logical_and.reduce([np.isfinite(scores[key]) for key in components])
        # Rank each component in its original contemporaneous eligible universe.
        # Keep a missing combined score whenever either component is absent.
        combined = sum(weight*rank(scores[key], eligible) for key, weight in zip(components, weights))
        result[name] = np.where(common, combined, np.nan)
    return result


def prediction_horizons(spec, name):
    if name not in spec['models']:
        return 'not_applicable'
    values = spec['model_horizons'][name]
    if not values or values != sorted(set(values)) or any(type(v) is not int or v < 1 for v in values):
        raise ValueError('Invalid prediction horizon metadata')
    return '+'.join(map(str, values))


def validate_study(study):
    models = set(study['models'])
    variants, references, blends = (set(study[key]) for key in ('variants', 'prediction_reference_map', 'score_blends'))
    if (models != variants | references | blends or len(models) != len(study['models'])
            or variants & references or variants & blends or references & blends
            or study['training_years'] != 3 or type(study['label_horizon']) is not int or study['label_horizon'] < 1
            or set(study['model_horizons']) != models):
        raise ValueError('Invalid horizon study model or training contract')
    for name in models:
        prediction_horizons(study, name)
    for name in variants:
        if study['model_horizons'][name] != [study['label_horizon']]:
            raise ValueError('New model horizon differs from its training labels')
    for name, blend in study['score_blends'].items():
        if not set(blend['components']) <= variants | references:
            raise ValueError('Blend requires declared base predictions')
        expected = sorted({h for component in blend['components'] for h in study['model_horizons'][component]})
        if study['model_horizons'][name] != expected:
            raise ValueError('Blend horizon provenance differs from its components')
    # Validate fixed weights before any expensive training starts.
    dummy = {name: np.ones((1, 1)) for name in variants | references}
    blend_scores(dummy, np.ones((1, 1), bool), study['score_blends'])
