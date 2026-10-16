"""Fit fixed financial value/context controls and paired seed repetitions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import shutil

import numpy as np

from quant_adaptation import walk_forward_adaptation
from quant_financial_learning import load_verified_panel
from quant_financial_controls import feature_view, validate_study
from quant_tree_learning import TREE_PARAMS, extended_features
from run_quant_cycle import protocol as base_protocol
from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'forecasts', 'reference-experiment', 'financial-panel',
                 'financial-verification', 'study-config', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--learner-compatibility', type=Path, required=True)
    args = parser.parse_args()
    study = json.loads(args.study_config.read_text())
    validate_study(study)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out/'COMPLETE.json').exists():
        raise ValueError('Completed predictions are immutable; choose another directory')
    spec = base_protocol(args.dataset, args.forecasts)
    reference = args.reference_experiment
    ref_spec = json.loads((reference/'PROTOCOL.json').read_text())
    ref_done = json.loads((reference/'COMPLETE.json').read_text())
    ref_receipt = json.loads((reference/'PREDICTION_RECEIPT.json').read_text())
    if (ref_done['protocol_sha256'] != checksum(reference/'PROTOCOL.json')
            or ref_receipt['protocol_sha256'] != ref_done['protocol_sha256']
            or checksum(reference/'PREDICTIONS.npz') != ref_receipt['sha256']
            or any(ref_spec[k] != spec[k] for k in ('source', 'forecast_sha256', 'test_years', 'primary_since'))
            or ref_spec['model_horizons']['annual_tree_blend'] != [study['label_horizon']]):
        raise ValueError('Annual baseline provenance differs from the financial study')
    for name, digest in ref_spec['code'].items():
        if checksum(reference/'code-at-execution'/name) != digest:
            raise ValueError('Annual baseline construction code is not intact')
    from verify_learner_defaults import read_compatibility
    spec['learner_compatibility'] = read_compatibility(args.learner_compatibility, reference)
    if ref_spec['tree_parameters'] != TREE_PARAMS:
        raise ValueError('Original learner parameters changed')
    data = load_dataset(args.dataset, 'all')
    financials, financial_receipt = load_verified_panel(args.financial_panel, args.financial_verification, args.dataset, data)
    spec.update(version='financial-controls-predictions-v1', study_config_sha256=checksum(args.study_config),
        horizon=study['label_horizon'], training_variants=study['variants'], financial_input=financial_receipt,
        **{key: study[key] for key in ('models', 'allocations', 'cohorts', 'cost_bps_per_side', 'training_years',
            'validation_sessions', 'prediction_reference_map', 'score_blends', 'model_horizons',
            'diagnostic_horizons', 'diagnostic_next_open', 'selection', 'feature_views', 'tree_seeds_by_model', 'seeds', 'context_scales')},
        tree_parameters=TREE_PARAMS, maximum_boosting_rounds=400, early_stopping_rounds=40,
        annual_reference_predictions_sha256=ref_receipt['sha256'],
        annual_reference_protocol_sha256=checksum(reference/'PROTOCOL.json'),
        training_status_by_year={str(y): 'purged_annual_discovery_only' for y in spec['test_years']},
        packages={n: importlib.metadata.version(n) for n in ('numpy', 'pandas', 'scipy', 'lightgbm')},
        comparisons=len(study['models'])*len(study['allocations']), account_win_rate_horizon=20,
        training_weight_contract='Unchanged equal influence per signal day; no recency decay; three-model rank mean.',
        financial_scope='Supplier public revision reconstruction, not actual historical system knowledge. Financial coverage never changes the eligible stock universe.')
    spec['source_gaps'].append('Financial supplier revisions are conservative reconstructions, not certified original filings at every historic date.')
    spec['research_sources'] += ['https://link.springer.com/article/10.1007/s11142-018-9470-2', 'https://proceedings.mlsys.org/paper_files/paper/2021/hash/0184b0cd3cfb185989f858a1d9f5c1eb-Abstract.html']
    code = Path(__file__).parent
    spec['code'].update({name: checksum(code/name) for name in ('run_financial_controls.py', 'quant_financial_learning.py', 'quant_financial_controls.py',
        'quant_adaptation.py', 'quant_tree_learning.py', 'financial_history.py', 'horizon_signals.py', 'verify_adaptation_cycle.py',
        'verify_financial_ablation.py', 'verify_financial_controls.py', 'verify_learner_defaults.py')})
    path = out/'PROTOCOL.json'
    if path.exists() and json.loads(path.read_text()) != spec:
        raise ValueError('Inputs, code or predeclared study changed; cannot resume')
    dump(path, spec)
    protocol_hash = checksum(path)
    frozen = out/'code-at-execution'
    frozen.mkdir(exist_ok=True)
    for name, digest in spec['code'].items():
        shutil.copyfile(code/name, frozen/name)
        if checksum(frozen/name) != digest:
            raise ValueError('Code changed while freezing')
    shutil.copyfile(args.study_config, out/'STUDY_CONFIG.json')
    started = datetime.now(timezone.utc).isoformat()
    dump(out/'RUNNING.json', {'started_at': started, 'protocol_sha256': protocol_hash})
    print(json.dumps({'phase': 'protocol_frozen', 'primary_since': spec['primary_since'],
        'variants': list(study['variants']), 'new_annual_ensembles': len(study['variants'])*len(spec['test_years'])}), flush=True)
    features = prepare_features(data)
    with np.load(args.forecasts, allow_pickle=False) as z:
        if not np.array_equal(z['dates'], data['dates']) or not np.array_equal(z['symbols'], data['symbols']):
            raise ValueError('Forecast identity does not match market data')
        forecasts = {key: z[key] for key in ('value', 'revision')}
    base, base_names = extended_features(data, features, forecasts)
    del forecasts
    if len(base_names) != study['baseline_features']:
        raise ValueError('Baseline feature count changed')
    scores, folds, definitions = {}, [], {}
    for variant, config in study['variants'].items():
        view = study['feature_views'][variant]
        matrix, names = feature_view(base, base_names, features['eligible'], financials, view)
        definitions[variant] = {'names': names, 'shape': list(matrix.shape), 'original_features_unchanged': True}
        dump(out/'FEATURES.json', definitions)
        print(json.dumps({'phase': 'features', 'variant': variant, 'features': len(names)}), flush=True)
        current, fitted = walk_forward_adaptation(data, features, matrix, names, spec['test_years'],
            {variant: study['variants'][variant]}, out, protocol_hash, horizon=spec['horizon'],
            training_years=study['training_years'], validation_sessions=study['validation_sessions'],
            callback=lambda r: print(json.dumps(r), flush=True))
        scores.update(current)
        folds.extend(fitted)
        dump(out/'TRAINING_FOLDS.json', folds)
        del matrix
    del base, financials
    with np.load(reference/'PREDICTIONS.npz', allow_pickle=False) as z:
        for name, original in study['prediction_reference_map'].items():
            scores[name] = z[original]
    if set(scores) != set(study['models']):
        raise ValueError('Prediction families differ from the predeclared study')
    np.savez_compressed(out/'PREDICTIONS.npz', **scores)
    receipt = {'sha256': checksum(out/'PREDICTIONS.npz'), 'protocol_sha256': protocol_hash}
    dump(out/'PREDICTION_RECEIPT.json', receipt)
    dump(out/'COMPLETE.json', {'status': 'adaptation_predictions_complete', 'started_at': started,
        'completed_at': datetime.now(timezone.utc).isoformat(), 'protocol_sha256': protocol_hash,
        'prediction_receipt': receipt, 'completed_training_folds': len(folds),
        'fitted_ridges': len(folds), 'fitted_tree_models': len(folds)*2, 'production_approved': False})


if __name__ == '__main__':
    main()
