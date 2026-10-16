"""Freeze periodic-refit predictions for separate, identical portfolio replay."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import shutil
import numpy as np

from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features
from run_quant_cycle import protocol as base_protocol
from quant_tree_learning import TREE_PARAMS, extended_features
from quant_adaptation import walk_forward_adaptation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--forecasts', type=Path, required=True)
    parser.add_argument('--reference-experiment', type=Path, required=True)
    parser.add_argument('--study-config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'COMPLETE.json').exists():
        raise ValueError('Completed predictions are immutable; choose another directory')
    study = json.loads(args.study_config.read_text())
    if study['training_years'] != 3 or set(study['models']) != set(study['variants']) | {'annual_tree_blend', 'annual_ridge_extended'}:
        raise ValueError('study model or training-window contract differs')
    spec = base_protocol(args.dataset, args.forecasts)
    reference = args.reference_experiment
    ref_spec = json.loads((reference / 'PROTOCOL.json').read_text())
    ref_done = json.loads((reference / 'COMPLETE.json').read_text())
    ref_receipt = json.loads((reference / 'PREDICTION_RECEIPT.json').read_text())
    if (ref_done['protocol_sha256'] != checksum(reference / 'PROTOCOL.json')
            or ref_receipt['protocol_sha256'] != ref_done['protocol_sha256']
            or checksum(reference / 'PREDICTIONS.npz') != ref_receipt['sha256']
            or ref_spec['source'] != spec['source'] or ref_spec['forecast_sha256'] != spec['forecast_sha256']
            or ref_spec['test_years'] != spec['test_years']):
        raise ValueError('Annual reference provenance differs from this study')
    for name, digest in ref_spec['code'].items():
        if checksum(reference / 'code-at-execution' / name) != digest:
            raise ValueError('Annual reference code is not intact')
    spec.update(version='rolling-adaptation-predictions-v1', study_config_sha256=checksum(args.study_config),
        models=study['models'], training_variants=study['variants'], allocations=study['allocations'], cohorts=study['cohorts'],
        tree_parameters=TREE_PARAMS, validation_sessions=study['validation_sessions'],
        maximum_boosting_rounds=400, early_stopping_rounds=40,
        annual_reference_predictions_sha256=ref_receipt['sha256'], annual_reference_protocol_sha256=checksum(reference / 'PROTOCOL.json'),
        training_status_by_year={str(y): 'purged_rolling_refits_discovery_only' for y in spec['test_years']},
        packages={n: importlib.metadata.version(n) for n in ('numpy', 'pandas', 'scipy', 'lightgbm')},
        selection=study['selection'], comparisons=len(study['models'])*len(study['allocations']),
        training_weight_contract='Equal total influence per signal day; optional 252-session training-loss decay. Validation retains equal daily weights. Training-only ridge transforms remain unweighted.',
        adaptation_scope='Rolling refits, not DoubleAdapt meta-learning reproduction; all historical comparisons remain exploratory.')
    code = Path(__file__).parent
    spec['code'].update({name: checksum(code / name) for name in
                        ('run_adaptation_cycle.py', 'quant_adaptation.py', 'quant_tree_learning.py')})
    spec['research_sources'] += ['https://arxiv.org/abs/2306.09862', 'https://github.com/SJTU-DMTai/DoubleAdapt']
    path = out / 'PROTOCOL.json'
    if path.exists() and json.loads(path.read_text()) != spec:
        raise ValueError('Inputs, code or predeclared study changed; cannot resume')
    dump(path, spec)
    protocol_hash = checksum(path)
    frozen = out / 'code-at-execution'
    frozen.mkdir(exist_ok=True)
    for name, digest in spec['code'].items():
        shutil.copyfile(code / name, frozen / name)
        if checksum(frozen / name) != digest:
            raise ValueError('Code changed while freezing')
    shutil.copyfile(args.study_config, out / 'STUDY_CONFIG.json')
    started = datetime.now(timezone.utc).isoformat()
    dump(out / 'RUNNING.json', {'started_at': started, 'protocol_sha256': protocol_hash})
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    with np.load(args.forecasts, allow_pickle=False) as z:
        if not np.array_equal(z['dates'], data['dates']) or not np.array_equal(z['symbols'], data['symbols']):
            raise ValueError('Forecast identity does not match market data')
        forecasts = {key: z[key] for key in ('value', 'revision')}
    matrix, names = extended_features(data, features, forecasts)
    del forecasts
    dump(out / 'FEATURES.json', {'names': names, 'shape': list(matrix.shape), 'missing_forecasts_exclude_security': False})
    print(json.dumps({'phase': 'protocol_frozen', 'variants': study['variants'], 'primary_since': spec['primary_since']}), flush=True)
    scores, folds = walk_forward_adaptation(data, features, matrix, names, spec['test_years'], study['variants'],
        out, protocol_hash, horizon=spec['horizon'], training_years=study['training_years'],
        validation_sessions=study['validation_sessions'], callback=lambda r: print(json.dumps(r), flush=True))
    del matrix
    with np.load(reference / 'PREDICTIONS.npz', allow_pickle=False) as z:
        scores['annual_tree_blend'] = z['tree_blend']
        scores['annual_ridge_extended'] = z['ridge_extended']
    controls = []
    for current, original in [('quarterly_tree_blend', 'annual_tree_blend'), ('monthly_ridge_extended', 'annual_ridge_extended')]:
        first = next(r for r in folds if r['variant'] == current)
        section = slice(first['signal_begin'], first['signal_end_exclusive'])
        if not np.allclose(scores[current][section], scores[original][section], equal_nan=True, atol=1e-10, rtol=0):
            raise ValueError('First uniform refit differs from the identical annual training reference')
        controls.append({'model': current, 'reference': original, 'first_period': first['period_start'], 'passed': True})
    dump(out / 'REFERENCE_REGRESSION.json', controls)
    for name, score in scores.items():
        if score.shape != data['close'].shape:
            raise ValueError('Prediction shape differs from market')
    prediction_path = out / 'PREDICTIONS.npz'
    np.savez_compressed(prediction_path, **scores)
    receipt = {'sha256': checksum(prediction_path), 'protocol_sha256': protocol_hash}
    dump(out / 'PREDICTION_RECEIPT.json', receipt)
    dump(out / 'COMPLETE.json', {'status': 'adaptation_predictions_complete', 'started_at': started,
        'completed_at': datetime.now(timezone.utc).isoformat(), 'protocol_sha256': protocol_hash,
        'prediction_receipt': receipt, 'completed_training_folds': len(folds), 'production_approved': False})


if __name__ == '__main__':
    main()
