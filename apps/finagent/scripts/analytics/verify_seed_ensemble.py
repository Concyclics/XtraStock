"""Recheck source chronology and reconstruct every seed aggregate independently."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from run_seed_ensemble import reference_inputs
from seed_ensemble import verify_combinations
from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features
from verify_adaptation_cycle import audit_predictions, predictive_diagnostics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('experiment', 'dataset', 'forecasts', 'reference-experiment', 'reference-parent',
                 'reference-verification', 'report'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    out = args.experiment
    spec = json.loads((out/'PROTOCOL.json').read_text())
    study = json.loads((out/'STUDY_CONFIG.json').read_text())
    reference_inputs(args.dataset, args.forecasts, args.reference_experiment, args.reference_verification, study)
    for name, source, key in [
        ('REFERENCE_TRAINING_FOLDS.json', args.reference_experiment/'TRAINING_FOLDS.json', 'reference_training_folds_sha256'),
        ('REFERENCE_VERIFICATION.json', args.reference_verification, 'reference_verification_sha256')]:
        if checksum(out/name) != spec[key] or checksum(source) != spec[key]:
            raise ValueError('Source chronology or independent verification evidence changed')
    if spec['aggregation'] != study['aggregation'] or spec['seeds'] != study['seeds'] or spec['model_refitted']:
        raise ValueError('Fixed seed aggregation contract changed')
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    _, _, source_audit = audit_predictions(args.reference_experiment, data, features, args.reference_parent)
    if source_audit['folds'] != 24:
        raise ValueError('Source annual fold provenance is incomplete')
    scores, outcome, report = audit_predictions(out, data, features, args.reference_experiment)
    if report['folds'] != 0:
        raise ValueError('Aggregation must not add a fitted model')
    first = int(np.searchsorted(data['dates'], f"{min(spec['test_years'])}-01-01"))
    last = int(np.searchsorted(data['dates'], f"{max(spec['test_years'])+1}-01-01"))
    expected = features['eligible'].copy()
    expected[:first-1] = False
    expected[last:] = False
    if any(not np.array_equal(np.isfinite(value), expected) for value in scores.values()):
        raise ValueError('Source or aggregate score calendar has missing or extra predictions')
    checks = verify_combinations(scores, features['eligible'], study)
    rows = predictive_diagnostics(data, features, scores, outcome, spec['test_years'], 20, next_open=True)
    pd.DataFrame(rows).to_csv(out/'PREDICTIVE_METRICS.csv', index=False)
    report.update(status='seed_ensembles_verified', source_fold_checks=source_audit['folds'],
        source_chronology_verified=True, new_model_fits=0, copied_families=6,
        independent_seed_aggregates=checks, diagnostics_rows=len(rows))
    dump(args.report, report)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
