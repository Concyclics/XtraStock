"""Compare predeclared portfolio constructions using frozen prior predictions.

No model refitting or test-return-based choice. Each capital level is an
independent run with its own same-capital benchmarks and verification records.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np

from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features
from run_quant_cycle import evaluate_scores

def protocol(dataset, reference, capital, members, study_path):
    if not np.isfinite(capital) or capital <= 0:
        raise ValueError('capital must be positive and finite')
    study = json.loads(study_path.read_text())
    if capital not in study['capital_levels']:
        raise ValueError('capital is not predeclared in the study configuration')
    original = reference / 'PROTOCOL.json'
    spec = json.loads(original.read_text())
    completed = json.loads((reference / 'COMPLETE.json').read_text())
    receipt = json.loads((reference / 'PREDICTION_RECEIPT.json').read_text())
    if (completed['protocol_sha256'] != checksum(original) or receipt['protocol_sha256'] != checksum(original)
            or receipt['sha256'] != checksum(reference / 'PREDICTIONS.npz')):
        raise ValueError('Reference prediction provenance failed')
    for name, digest in spec['source'].items():
        if checksum(dataset / name) != digest:
            raise ValueError('Reference and current market panels differ: ' + name)
    for name, digest in spec['code'].items():
        if checksum(reference / 'code-at-execution' / name) != digest:
            raise ValueError('Reference frozen code differs: ' + name)
    code = Path(__file__).parent
    spec.update(version='portfolio-cycle-v1', portfolio_evaluation_contract=1, capital=capital,
        models=study['models'], cohorts=study['cohorts'], allocations=study['allocations'],
        execution_audit=bool(study.get('execution_audit', False)),
        cost_bps_per_side=study.get('cost_bps_per_side', spec['cost_bps_per_side']),
        comparisons=len(study['models'])*len(study['allocations']), study_config_sha256=checksum(study_path),
        cohort_members_sha256=checksum(members), reference_protocol_sha256=checksum(original),
        reference_predictions_sha256=receipt['sha256'], reference_training_folds_sha256=checksum(reference / 'TRAINING_FOLDS.json'),
        model_refitted=False,
        selection=f"{len(study['models'])} frozen scores; {len(study['allocations'])} fixed constructions; all cohorts/costs reported. No return-based selection or automatic promotion.",
        scope='Previously inspected historical reconstruction; all-universe is an operational projection, not an untouched holdout.',
        benchmark_note='Equal-weight benchmark uses fractional shares as a reference; model portfolios retain round-lot constraints.',
        code={name: checksum(code/name) for name in ('run_portfolio_cycle.py', 'run_quant_cycle.py',
             'strategy_research.py', 'strategy_dataset.py', 'quant_learning.py', 'execution_diagnostics.py', 'verify_quant_cycle.py')})
    if study.get('market_risk_model'):
        from market_risk_forecast import validate_config
        validate_config(study['market_risk_model'])
        spec['market_risk_model'] = study['market_risk_model']
        spec['code']['market_risk_forecast.py'] = checksum(code/'market_risk_forecast.py')
    return spec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--reference-experiment', type=Path, required=True)
    parser.add_argument('--capital', type=float, required=True)
    parser.add_argument('--study-config', type=Path,
                        default=Path(__file__).resolve().parents[4] / 'config/research/portfolio.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'COMPLETE.json').exists():
        raise SystemExit('Completed experiment is immutable; choose a new directory')
    reference = args.reference_experiment.resolve()
    study = json.loads(args.study_config.read_text())
    assets = json.loads((args.dataset / 'assets.json').read_text())
    members = {cohort: sorted(a['symbol'] for a in assets if cohort == 'all' or a['cohort'] == cohort)
               for cohort in study['cohorts']}
    if any(not values or len(values) != len(set(values)) for values in members.values()):
        raise ValueError('Invalid evaluation membership')
    member_path = out / 'COHORT_MEMBERS.json'
    if member_path.exists():
        if json.loads(member_path.read_text()) != members:
            raise ValueError('Cohort membership changed; cannot resume')
    else:
        dump(member_path, members)
    spec = protocol(args.dataset, reference, args.capital, member_path, args.study_config)
    spec_path = out / 'PROTOCOL.json'
    if spec_path.exists() and json.loads(spec_path.read_text()) != spec:
        raise ValueError('Input, code or protocol changed; cannot resume')
    dump(spec_path, spec)
    protocol_hash = checksum(spec_path)
    frozen = out / 'code-at-execution'
    frozen.mkdir(exist_ok=True)
    for name, digest in spec['code'].items():
        shutil.copyfile(Path(__file__).parent / name, frozen / name)
        if checksum(frozen / name) != digest:
            raise ValueError('Code changed during freeze')
    shutil.copyfile(reference / 'PROTOCOL.json', out / 'REFERENCE_PROTOCOL.json')
    shutil.copyfile(args.study_config, out / 'STUDY_CONFIG.json')
    shutil.copyfile(reference / 'TRAINING_FOLDS.json', out / 'TRAINING_FOLDS.json')
    prediction_path = out / 'PREDICTIONS.npz'
    if prediction_path.exists():
        if checksum(prediction_path) != spec['reference_predictions_sha256']:
            raise ValueError('Prediction copy changed')
    else:
        shutil.copyfile(reference / 'PREDICTIONS.npz', prediction_path)
    if checksum(prediction_path) != spec['reference_predictions_sha256']:
        raise ValueError('Prediction copy verification failed')
    dump(out / 'PREDICTION_RECEIPT.json', {'sha256': spec['reference_predictions_sha256'], 'protocol_sha256': protocol_hash})
    started = datetime.now(timezone.utc).isoformat()
    dump(out / 'RUNNING.json', {'started_at': started, 'protocol_sha256': protocol_hash, 'model_refitted': False})
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    if spec.get('market_risk_model'):
        from market_risk_forecast import market_inputs, build_artifacts
        features['market_risk_forecasts'] = build_artifacts(out, market_inputs(data, features), spec, protocol_hash)
    with np.load(prediction_path, allow_pickle=False) as source:
        scores = {name: source[name] for name in spec['models']}
    if any(score.shape != data['close'].shape for score in scores.values()):
        raise ValueError('Prediction shape differs from verified reference market')
    replays = (2 + len(spec['models'])*len(spec['allocations'])) * len(spec['cohorts']) * len(spec['cost_bps_per_side'])
    print(json.dumps({'phase': 'protocol_frozen', 'capital': args.capital, 'replays': replays,
                      'model_refitted': False, 'cohorts': spec['cohorts']}), flush=True)
    evaluate_scores(data, features, scores, spec, out, started, protocol_hash)


if __name__ == '__main__':
    main()
