"""Reconstruct all financial features and predictions from the saved fitted models."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import rankdata

from financial_history import FEATURE_NAMES
from quant_financial_learning import append_financial_features, load_verified_panel, validate_study
from quant_tree_learning import extended_features
from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features
from verify_adaptation_cycle import audit_predictions, predictive_diagnostics


def reference_ranks(values, eligible):
    values = np.where(eligible & np.isfinite(values), values, np.nan)
    result = rankdata(values, axis=1, method='average', nan_policy='omit')
    counts = np.isfinite(values).sum(axis=1)
    return np.divide(result, counts[:, None], out=np.full_like(result, np.nan), where=counts[:, None] > 0)


def reference_extras(values, eligible, group):
    columns = []
    for name in group:
        field = values[..., FEATURE_NAMES.index(name)]
        finite = np.isfinite(field) & eligible
        columns.extend([reference_ranks(field, eligible), finite.astype(float)])
    for index, divisor in [(-2, 12), (-1, 550)]:
        field = values[..., index]
        finite = eligible & np.isfinite(field)
        columns.extend([np.where(finite, field/divisor, np.nan), finite.astype(float)])
    return np.stack(columns, axis=-1)


def replay_ridge(model, matrix):
    median, mean, scale, coefficient = [np.asarray(model[key]) for key in ('median', 'mean', 'scale', 'coefficient')]
    filled = np.where(np.isfinite(matrix), matrix, median)
    return np.clip((filled-mean)/scale, -8, 8) @ coefficient[1:] + coefficient[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('experiment', 'dataset', 'forecasts', 'reference-experiment', 'financial-panel', 'financial-verification', 'report'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    out = args.experiment
    spec = json.loads((out/'PROTOCOL.json').read_text())
    study = json.loads((out/'STUDY_CONFIG.json').read_text())
    validate_study(study)
    if any(spec[key] != study[key] for key in ('financial_groups', 'context_scales')):
        raise ValueError('Financial feature specification differs')
    for name, expected in spec['source'].items():
        if checksum(args.dataset/name) != expected:
            raise ValueError('Frozen market changed')
    if checksum(args.forecasts) != spec['forecast_sha256']:
        raise ValueError('Frozen earnings forecasts changed')
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
    for variant, group in study['financial_groups'].items():
        matrix, names = append_financial_features(base, base_names, features['eligible'], values, group)
        independent = reference_extras(values, features['eligible'], group).astype(matrix.dtype)
        if (not np.array_equal(matrix[..., :len(base_names)], base, equal_nan=True)
                or not np.array_equal(matrix[..., len(base_names):], independent, equal_nan=True)
                or definitions[variant] != {'names': names, 'shape': list(matrix.shape), 'original_features_unchanged': True}):
            raise ValueError('Financial feature transforms differ from independent calculations')
        # Replay using independently computed financial columns, rather than
        # trusting the original feature implementation a second time.
        matrix[..., len(base_names):] = independent
        del independent
        for fold in [f for f in folds if f['variant'] == variant]:
            begin, end = fold['signal_begin'], fold['signal_end_exclusive']
            eligible = features['eligible'][begin:end]
            ii, jj = np.where(eligible)
            components = [np.full(eligible.shape, np.nan) for _ in range(3)]
            directory = out/'folds'/variant
            trees = [lgb.Booster(model_file=str(directory/f"{fold['period_start']}-{key}.txt")) for key in ('tree_return', 'tree_rank')]
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
                'predictions': int(eligible.sum()), 'maximum_score_difference': float(np.nanmax(np.abs(replayed-expected))),
                'baseline_columns_unchanged': True, 'all_financial_columns_match': True, 'all_model_scores_match': True})
            print(json.dumps({'phase': 'independent_model_replay', **checks[-1]}), flush=True)
        del matrix
    rows = predictive_diagnostics(data, features, scores, outcome, spec['test_years'], 20, next_open=True)
    pd.DataFrame(rows).to_csv(out/'PREDICTIVE_METRICS.csv', index=False)
    report.update(status='financial_predictions_verified', feature_model_checks=checks,
        rebuilt_from_saved_models=True, independent_financial_transforms=True, diagnostics_rows=len(rows))
    dump(args.report, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('checks', 'feature_model_checks')}))


if __name__ == '__main__':
    main()
