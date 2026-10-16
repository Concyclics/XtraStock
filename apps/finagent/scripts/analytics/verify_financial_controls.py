"""Verify financial value/context controls, actual fitted seeds and every saved prediction."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant_financial_learning import load_verified_panel
from quant_financial_controls import feature_view, independent_view, validate_study
from verify_learner_defaults import read_compatibility
from quant_tree_learning import extended_features
from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features
from verify_adaptation_cycle import audit_predictions, predictive_diagnostics
from verify_financial_ablation import reference_ranks, replay_ridge


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('experiment', 'dataset', 'forecasts', 'reference-experiment', 'financial-panel', 'financial-verification', 'learner-compatibility', 'report'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    out = args.experiment
    spec = json.loads((out/'PROTOCOL.json').read_text())
    study = json.loads((out/'STUDY_CONFIG.json').read_text())
    validate_study(study)
    if any(spec[key] != study[key] for key in ('feature_views', 'tree_seeds_by_model', 'seeds', 'context_scales')):
        raise ValueError('Financial feature specification differs')
    for name, expected in spec['source'].items():
        if checksum(args.dataset/name) != expected:
            raise ValueError('Frozen market changed')
    if checksum(args.forecasts) != spec['forecast_sha256']:
        raise ValueError('Frozen earnings forecasts changed')
    if read_compatibility(args.learner_compatibility, args.reference_experiment) != spec['learner_compatibility']:
        raise ValueError('Learner compatibility evidence changed')
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    scores, outcome, report = audit_predictions(out, data, features, args.reference_experiment)
    values, financial_receipt = load_verified_panel(args.financial_panel, args.financial_verification, args.dataset, data)
    if financial_receipt != spec['financial_input']:
        raise ValueError('Financial source differs from training')
    with np.load(args.forecasts, allow_pickle=False) as z:
        if not np.array_equal(z['dates'], data['dates']) or not np.array_equal(z['symbols'], data['symbols']):
            raise ValueError('Forecast axes changed')
        base, base_names = extended_features(data, features, {key: z[key] for key in ('value', 'revision')})
    if len(base_names) != study['baseline_features']:
        raise ValueError('Original feature count changed')
    definitions = json.loads((out/'FEATURES.json').read_text())
    folds = json.loads((out/'TRAINING_FOLDS.json').read_text())
    import lightgbm as lgb
    checks = []
    for variant in study['variants']:
        view = study['feature_views'][variant]
        matrix, names = feature_view(base, base_names, features['eligible'], values, view)
        independent = independent_view(base, base_names, features['eligible'], values, view)
        if (not np.array_equal(matrix[..., :len(base_names)], base, equal_nan=True)
                or not np.array_equal(matrix, independent, equal_nan=True)
                or definitions[variant] != {'names': names, 'shape': list(matrix.shape), 'original_features_unchanged': True}):
            raise ValueError('Financial feature transforms differ from independent calculations')
        # Replay using independently computed financial columns, rather than
        # trusting the original feature implementation a second time.
        matrix = independent
        del independent
        for fold in [f for f in folds if f['variant'] == variant]:
            begin, end = fold['signal_begin'], fold['signal_end_exclusive']
            eligible = features['eligible'][begin:end]
            ii, jj = np.where(eligible)
            components = [np.full(eligible.shape, np.nan) for _ in range(3)]
            directory = out/'folds'/variant
            trees = [lgb.Booster(model_file=str(directory/f"{fold['period_start']}-{key}.txt")) for key in ('tree_return', 'tree_rank')]
            if any(tree.params.get('seed') != study['tree_seeds_by_model'][variant] for tree in trees):
                raise ValueError('Actual saved tree seed differs from predeclared model')
            if any(tree.feature_name() != names for tree in trees):
                raise ValueError('Saved tree feature order differs')
            for start in range(0, len(ii), 100000):
                ri, cj = ii[start:start+100000], jj[start:start+100000]
                x = matrix[ri+begin, cj]
                components[0][ri, cj] = replay_ridge(fold['models']['ridge_extended'], x)
                for slot, tree in enumerate(trees, 1):
                    components[slot][ri, cj] = tree.predict(x, num_threads=4)
            replayed = sum(reference_ranks(c, eligible) for c in components)/3
            expected = scores[variant][begin:end]
            if not np.allclose(replayed, expected, rtol=0, atol=1e-12, equal_nan=True):
                raise ValueError('Saved model replay differs: '+variant+'/'+fold['period_start'])
            checks.append({'model': variant, 'year': fold['year'], 'features': len(names),
                'feature_view': view, 'tree_seed': study['tree_seeds_by_model'][variant],
                'predictions': int(eligible.sum()), 'maximum_score_difference': float(np.nanmax(np.abs(replayed-expected))),
                'baseline_columns_unchanged': True, 'all_financial_columns_match': True, 'all_model_scores_match': True})
            print(json.dumps({'phase': 'independent_model_replay', **checks[-1]}), flush=True)
        del matrix
    # A seed change must not silently alter deterministic ridge fits.
    ridge_checks = []
    for view, left, right in [('original', 'annual_original_seed261017', 'annual_original_seed261018'),
                              ('profitability', 'annual_profitability_seed261017', 'annual_profitability_seed261018')]:
        for year in spec['test_years']:
            a, b = [next(f for f in folds if f['variant']==name and f['year']==year)['models']['ridge_extended'] for name in (left, right)]
            if a != b:
                raise ValueError('Only changing tree seed changed the ridge fit')
            ridge_checks.append({'view': view, 'year': year, 'ridge_parameters_identical': True})
    rows = predictive_diagnostics(data, features, scores, outcome, spec['test_years'], 20, next_open=True)
    pd.DataFrame(rows).to_csv(out/'PREDICTIVE_METRICS.csv', index=False)
    report.update(status='financial_controls_verified', feature_model_checks=checks,
        rebuilt_from_saved_models=True, independent_financial_transforms=True, actual_seeds_verified=True,
        ridge_seed_checks=ridge_checks, diagnostics_rows=len(rows))
    dump(args.report, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('checks', 'feature_model_checks')}))


if __name__ == '__main__':
    main()
