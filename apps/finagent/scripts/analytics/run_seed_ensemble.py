"""Aggregate all three frozen seed forecasts in each family; never refit or select seeds."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import shutil

import numpy as np

from run_quant_cycle import protocol as base_protocol
from seed_ensemble import combine, validate_reference, validate_study
from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features


def reference_inputs(dataset, forecasts, reference, verification, study):
    validate_study(study)
    base = base_protocol(dataset, forecasts)
    original = json.loads((reference/'PROTOCOL.json').read_text())
    done = json.loads((reference/'COMPLETE.json').read_text())
    receipt = json.loads((reference/'PREDICTION_RECEIPT.json').read_text())
    evidence = json.loads(verification.read_text())
    validate_reference(study, original, json.loads((reference/'STUDY_CONFIG.json').read_text()))
    if (done['status'] != 'adaptation_predictions_complete'
            or done['protocol_sha256'] != checksum(reference/'PROTOCOL.json')
            or receipt['protocol_sha256'] != done['protocol_sha256']
            or receipt['sha256'] != checksum(reference/'PREDICTIONS.npz')
            or any(original[k] != base[k] for k in ('source', 'forecast_sha256', 'test_years', 'primary_since'))
            or evidence['status'] != 'financial_controls_verified' or not evidence['actual_seeds_verified']
            or evidence['folds'] != 24 or len(evidence['feature_model_checks']) != 24
            or any(not r['all_model_scores_match'] for r in evidence['feature_model_checks'])):
        raise ValueError('Source prediction receipts or prior independent verification differ')
    for name, digest in original['code'].items():
        if checksum(reference/'code-at-execution'/name) != digest:
            raise ValueError('Source fitted-prediction construction code changed')
    return base, original, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'forecasts', 'reference-experiment', 'reference-verification', 'study-config', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    study = json.loads(args.study_config.read_text())
    spec, original, receipt = reference_inputs(args.dataset, args.forecasts, args.reference_experiment,
                                              args.reference_verification, study)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out/'COMPLETE.json').exists():
        raise ValueError('Completed predictions are immutable; choose another directory')
    spec.update(version='fixed-seed-rank-ensemble-v1', study_config_sha256=checksum(args.study_config),
        horizon=20, training_variants={}, model_refitted=False,
        **{key: study[key] for key in ('models', 'allocations', 'cohorts', 'cost_bps_per_side', 'training_years',
            'validation_sessions', 'prediction_reference_map', 'score_blends', 'model_horizons',
            'diagnostic_horizons', 'diagnostic_next_open', 'selection', 'seeds', 'aggregation')},
        annual_reference_predictions_sha256=receipt['sha256'],
        annual_reference_protocol_sha256=checksum(args.reference_experiment/'PROTOCOL.json'),
        reference_training_folds_sha256=checksum(args.reference_experiment/'TRAINING_FOLDS.json'),
        reference_verification_sha256=checksum(args.reference_verification),
        financial_input=original['financial_input'], source_gaps=original['source_gaps'],
        training_status_by_year={str(y): 'no_new_fit_fixed_seed_rank_mean_from_purged_annual_models' for y in spec['test_years']},
        packages={n: importlib.metadata.version(n) for n in ('numpy', 'pandas', 'scipy')},
        comparisons=len(study['models'])*len(study['allocations']), account_win_rate_horizon=20,
        aggregation_scope='Rank each seed forecast within its same-day eligible universe, then average all three with fixed equal weights. No performance-based seed or weight choice.')
    spec['research_sources'] = list(dict.fromkeys([*original['research_sources'], 'https://dachxiu.chicagobooth.edu/download/ML.pdf']))
    code = Path(__file__).parent
    spec['code'].update({name: checksum(code/name) for name in
        ('run_seed_ensemble.py', 'seed_ensemble.py', 'verify_seed_ensemble.py', 'horizon_signals.py', 'verify_adaptation_cycle.py')})
    path = out/'PROTOCOL.json'
    if path.exists() and json.loads(path.read_text()) != spec:
        raise ValueError('Inputs or fixed aggregation changed; cannot resume')
    dump(path, spec)
    digest = checksum(path)
    frozen = out/'code-at-execution'
    frozen.mkdir(exist_ok=True)
    for name, expected in spec['code'].items():
        shutil.copyfile(code/name, frozen/name)
        if checksum(frozen/name) != expected:
            raise ValueError('Code changed while freezing')
    for source, name in [(args.study_config, 'STUDY_CONFIG.json'),
            (args.reference_verification, 'REFERENCE_VERIFICATION.json'),
            (args.reference_experiment/'TRAINING_FOLDS.json', 'REFERENCE_TRAINING_FOLDS.json')]:
        shutil.copyfile(source, out/name)
    started = datetime.now(timezone.utc).isoformat()
    dump(out/'RUNNING.json', {'started_at': started, 'protocol_sha256': digest, 'model_refitted': False})
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    with np.load(args.reference_experiment/'PREDICTIONS.npz', allow_pickle=False) as z:
        scores = {name: z[old] for name, old in study['prediction_reference_map'].items()}
    scores.update(combine(scores, features['eligible'], study))
    dump(out/'TRAINING_FOLDS.json', [])
    np.savez_compressed(out/'PREDICTIONS.npz', **scores)
    receipt = {'sha256': checksum(out/'PREDICTIONS.npz'), 'protocol_sha256': digest}
    dump(out/'PREDICTION_RECEIPT.json', receipt)
    dump(out/'COMPLETE.json', {'status': 'adaptation_predictions_complete', 'started_at': started,
        'completed_at': datetime.now(timezone.utc).isoformat(), 'protocol_sha256': digest,
        'prediction_receipt': receipt, 'completed_training_folds': 0, 'model_refitted': False,
        'source_models_copied': 6, 'new_fixed_aggregates': 2, 'production_approved': False})
    print(json.dumps({'status': 'seed_aggregates_complete', 'source_models': 6, 'fixed_aggregates': 2, 'new_fits': 0}))


if __name__ == '__main__':
    main()
