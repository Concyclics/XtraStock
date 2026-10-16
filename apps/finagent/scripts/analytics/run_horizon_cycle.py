"""Train predeclared short-horizon scores and combine frozen horizon ranks.

Prediction labels and portfolio selection intervals are separate contracts.
This entrypoint trains predictions only; run_portfolio_cycle owns cash books.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import shutil

import numpy as np

from horizon_signals import blend_scores, validate_study
from quant_adaptation import walk_forward_adaptation
from quant_tree_learning import TREE_PARAMS, extended_features
from run_quant_cycle import protocol as base_protocol
from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'forecasts', 'reference-experiment', 'study-config', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    study = json.loads(args.study_config.read_text())
    validate_study(study)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'COMPLETE.json').exists():
        raise ValueError('Completed predictions are immutable; choose another directory')
    spec = base_protocol(args.dataset, args.forecasts)
    reference = args.reference_experiment
    ref_spec = json.loads((reference / 'PROTOCOL.json').read_text())
    ref_done = json.loads((reference / 'COMPLETE.json').read_text())
    ref_receipt = json.loads((reference / 'PREDICTION_RECEIPT.json').read_text())
    if (ref_done['protocol_sha256'] != checksum(reference / 'PROTOCOL.json')
            or ref_receipt['protocol_sha256'] != ref_done['protocol_sha256']
            or checksum(reference / 'PREDICTIONS.npz') != ref_receipt['sha256']
            or ref_spec['source'] != spec['source'] or ref_spec['forecast_sha256'] != spec['forecast_sha256']
            or ref_spec['test_years'] != spec['test_years'] or ref_spec['primary_since'] != spec['primary_since']):
        raise ValueError('Annual reference provenance differs from this study')
    for name, digest in ref_spec['code'].items():
        if checksum(reference / 'code-at-execution' / name) != digest:
            raise ValueError('Annual reference code is not intact')
    for name, old in study['prediction_reference_map'].items():
        expected = ref_spec.get('model_horizons', {}).get(old, [ref_spec['horizon']])
        if study['model_horizons'][name] != expected:
            raise ValueError('Reference label horizon differs from the study')
    spec.update(version='fixed-horizon-predictions-v1', study_config_sha256=checksum(args.study_config),
        horizon=study['label_horizon'], training_variants=study['variants'],
        **{key: study[key] for key in ('models', 'allocations', 'cohorts', 'cost_bps_per_side', 'training_years',
            'validation_sessions', 'prediction_reference_map', 'score_blends', 'model_horizons',
            'diagnostic_horizons', 'diagnostic_next_open', 'selection')},
        tree_parameters=TREE_PARAMS, maximum_boosting_rounds=400, early_stopping_rounds=40,
        annual_reference_predictions_sha256=ref_receipt['sha256'],
        annual_reference_protocol_sha256=checksum(reference / 'PROTOCOL.json'),
        training_status_by_year={str(y): 'purged_annual_discovery_only' for y in spec['test_years']},
        packages={n: importlib.metadata.version(n) for n in ('numpy', 'pandas', 'scipy', 'lightgbm')},
        comparisons=len(study['models'])*len(study['allocations']), account_win_rate_horizon=20,
        training_weight_contract='Equal total influence per signal day; no recency decay. Validation retains equal daily weights.',
        horizon_scope='Fixed contemporaneous rank blends; no outcome-based mixture weights. Selection cadence does not force liquidation at label expiry.')
    code = Path(__file__).parent
    spec['code'].update({name: checksum(code / name) for name in
        ('run_horizon_cycle.py', 'horizon_signals.py', 'quant_adaptation.py', 'quant_tree_learning.py', 'verify_adaptation_cycle.py')})
    spec['research_sources'] += ['https://journal.fsv.cuni.cz/article/multi-horizon-equity-returns-predictability-via-machine-learning/',
                                'https://repub.eur.nl/pub/25718/AnotherLook_2011.pdf']
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
    print(json.dumps({'phase': 'protocol_frozen', 'label_horizon': spec['horizon'], 'primary_since': spec['primary_since']}), flush=True)
    scores, folds = walk_forward_adaptation(data, features, matrix, names, spec['test_years'], study['variants'],
        out, protocol_hash, horizon=spec['horizon'], training_years=study['training_years'],
        validation_sessions=study['validation_sessions'], callback=lambda r: print(json.dumps(r), flush=True))
    del matrix
    controls = []
    with np.load(reference / 'PREDICTIONS.npz', allow_pickle=False) as z:
        for name, original in study['prediction_reference_map'].items():
            scores[name] = z[original]
            controls.append({'model': name, 'reference': original, 'entire_prediction_array_copied': True})
    scores.update(blend_scores(scores, features['eligible'], study['score_blends']))
    if set(scores) != set(study['models']) or any(score.shape != data['close'].shape for score in scores.values()):
        raise ValueError('Prediction families or shapes differ from the declared study')
    dump(out / 'REFERENCE_REGRESSION.json', controls)
    prediction_path = out / 'PREDICTIONS.npz'
    np.savez_compressed(prediction_path, **scores)
    receipt = {'sha256': checksum(prediction_path), 'protocol_sha256': protocol_hash}
    dump(out / 'PREDICTION_RECEIPT.json', receipt)
    dump(out / 'COMPLETE.json', {'status': 'adaptation_predictions_complete', 'started_at': started,
        'completed_at': datetime.now(timezone.utc).isoformat(), 'protocol_sha256': protocol_hash,
        'prediction_receipt': receipt, 'completed_training_folds': len(folds), 'production_approved': False})


if __name__ == '__main__':
    main()
