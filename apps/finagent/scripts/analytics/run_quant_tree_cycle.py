"""Run a fixed extended-feature/tree ablation against frozen ridge predictions."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import numpy as np

from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features
from run_quant_cycle import protocol as ridge_protocol, evaluate_scores
from quant_tree_learning import MODEL_NAMES, TREE_PARAMS, extended_features, walk_forward_trees


def protocol(dataset, forecasts, reference):
    spec = ridge_protocol(dataset, forecasts)
    code = Path(__file__).parent
    reference_receipt = json.loads((reference.parent / 'PREDICTION_RECEIPT.json').read_text())
    reference_protocol = reference.parent / 'PROTOCOL.json'
    if (checksum(reference) != reference_receipt['sha256']
            or checksum(reference_protocol) != reference_receipt['protocol_sha256']
            or json.loads(reference_protocol.read_text())['source'] != spec['source']):
        raise ValueError('Reference predictions do not match their receipt or the current market panel')
    spec.update(version='quant-tree-cycle-v1', models=[*MODEL_NAMES, 'ridge_price_reference'],
                allocations={'equal_full': {}, 'inverse_full': {'weighting': 'inverse_volatility'}},
                comparisons=10, tree_parameters=TREE_PARAMS, validation_sessions=63,
                maximum_boosting_rounds=400, early_stopping_rounds=40,
                reference_prediction_sha256=checksum(reference),
                packages={name: importlib.metadata.version(name) for name in ('numpy', 'pandas', 'scipy', 'lightgbm')},
                selection='Four fixed new scores and one frozen original ridge score; all allocations reported.',
                label_contract='Next-open to close +20 sessions; discovery-only excess and rank targets; clipped training returns.')
    spec['code'].update({name: checksum(code / name) for name in
                        ('run_quant_tree_cycle.py', 'quant_tree_learning.py')})
    spec['research_sources'] += ['https://arxiv.org/abs/2009.11189',
                                'https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py']
    return spec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--forecasts', type=Path, required=True)
    parser.add_argument('--reference-predictions', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'COMPLETE.json').exists():
        raise SystemExit('Completed output is immutable; choose a new output directory')
    spec = protocol(args.dataset, args.forecasts, args.reference_predictions)
    protocol_path = out / 'PROTOCOL.json'
    if protocol_path.exists() and json.loads(protocol_path.read_text()) != spec:
        raise SystemExit('Inputs, code or protocol changed; cannot resume')
    dump(protocol_path, spec)
    protocol_hash = checksum(protocol_path)
    started = datetime.now(timezone.utc).isoformat()
    dump(out / 'RUNNING.json', {'started_at': started, 'protocol_sha256': protocol_hash})
    frozen = out / 'code-at-execution'
    frozen.mkdir(exist_ok=True)
    for name in spec['code']:
        (frozen / name).write_bytes((Path(__file__).parent / name).read_bytes())
        if checksum(frozen / name) != spec['code'][name]:
            raise ValueError('Source changed while freezing code')
    print(json.dumps({'phase': 'protocol_frozen', 'models': spec['models'], 'replays': 48}), flush=True)
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    predfile = out / 'PREDICTIONS.npz'
    if predfile.exists():
        receipt = json.loads((out / 'PREDICTION_RECEIPT.json').read_text())
        if receipt['sha256'] != checksum(predfile) or receipt['protocol_sha256'] != protocol_hash:
            raise ValueError('Prediction checkpoint mismatch')
        with np.load(predfile, allow_pickle=False) as source:
            scores = {key: source[key] for key in source.files}
    else:
        with np.load(args.forecasts, allow_pickle=False) as source:
            if not np.array_equal(source['dates'], data['dates']) or not np.array_equal(source['symbols'], data['symbols']):
                raise ValueError('Forecast panel identity mismatch')
            forecasts = {key: source[key] for key in ('value', 'revision')}
        matrix, names = extended_features(data, features, forecasts)
        del forecasts
        dump(out / 'FEATURES.json', {'names': names, 'count': len(names), 'shape': list(matrix.shape),
                                     'availability': 'Signal-day close; raw prices plus retrospectively adjusted ratios.',
                                     'missing_forecasts_exclude_security': False})
        folds = []

        def checkpoint(record):
            if record['phase'] == 'fold':
                folds.append(record)
                dump(out / 'TRAINING_FOLDS.json', folds)
                print(json.dumps({key: record[key] for key in ('phase', 'year', 'training_rows', 'training_dates',
                                                               'inner_training_dates', 'train_label_last')}), flush=True)
            else:
                print(json.dumps(record), flush=True)

        scores, _ = walk_forward_trees(data, features, matrix, names, spec['test_years'], out / 'models', callback=checkpoint)
        del matrix
        with np.load(args.reference_predictions, allow_pickle=False) as source:
            scores['ridge_price_reference'] = source['ridge_price']
        np.savez_compressed(predfile, **scores)
        dump(out / 'PREDICTION_RECEIPT.json', {'sha256': checksum(predfile), 'protocol_sha256': protocol_hash})
    evaluate_scores(data, features, scores, spec, out, started, protocol_hash)


if __name__ == '__main__':
    main()
