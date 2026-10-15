"""Independently recompute every reported NAV metric and check frozen inputs.

Passing these arithmetic checks does not resolve data completeness, market
execution, strategy selection bias, or authorize production promotion.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import gzip
import json
from pathlib import Path
import numpy as np
import pandas as pd
from strategy_dataset import checksum, dump


def verify(experiment):
    experiment = Path(experiment)
    protocol = json.loads((experiment / 'PROTOCOL.json').read_text())
    completed = json.loads((experiment / 'COMPLETE.json').read_text())
    if checksum(experiment / 'PROTOCOL.json') != completed['protocol_sha256']:
        raise ValueError('Protocol does not match completion record')
    for name, expected in protocol['code'].items():
        if checksum(experiment / 'code-at-execution' / name) != expected:
            raise ValueError('Frozen code differs from protocol: ' + name)
    receipt = json.loads((experiment / 'PREDICTION_RECEIPT.json').read_text())
    if (receipt['protocol_sha256'] != completed['protocol_sha256']
            or receipt['sha256'] != checksum(experiment / 'PREDICTIONS.npz')):
        raise ValueError('Prediction checkpoint differs from protocol')
    table = pd.read_csv(experiment / 'COSTED_RESULTS.csv')
    yearly = pd.read_csv(experiment / 'YEARLY_RESULTS.csv')
    expected = (2 + len(protocol['models']) * len(protocol['allocations'])) * len(protocol['cohorts']) * len(protocol['cost_bps_per_side'])
    keys = ['model', 'allocation', 'cohort', 'cost']
    if len(table) != expected or len(yearly) != expected * len(protocol['test_years']) or table.duplicated(keys).any():
        raise ValueError('Missing or duplicate experiment results')
    audit = []
    for row in table.to_dict('records'):
        path = experiment / 'simulations' / f"{row['cohort']}-cost{row['cost']}-{row['model']}-{row['allocation']}.json.gz"
        with gzip.open(path, 'rt') as stream:
            result = json.load(stream)
        if result.get('protocol_sha256') != completed['protocol_sha256']:
            raise ValueError('Simulation protocol differs')
        nav = np.asarray(result['nav'], float)
        dates = result['dates']
        if len(nav) != len(dates) + 1 or not np.all(np.isfinite(nav)) or np.any(nav <= 0):
            raise ValueError('Invalid NAV path')
        calendar_days = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days + 1
        checks = {'net_return': nav[-1] / nav[0] - 1,
                  'net_cagr': (nav[-1] / nav[0]) ** (365.25 / calendar_days) - 1,
                  'maximum_drawdown': -min(v / max(nav[:i+1]) - 1 for i, v in enumerate(nav)),
                  'period_win_rate': np.mean([nav[i+20] > nav[i] for i in range(0, len(nav)-20, 20)])}
        for metric, value in checks.items():
            if not np.isclose(row[metric], value, atol=1e-10, rtol=0):
                raise ValueError(f'{path.name}: {metric} differs from NAV')
        if any(s['signal_date'] >= s['date'] for s in result['rebalance_log']) or result['metrics']['minimum_cash'] < -.001:
            raise ValueError('Signal timing or cash accounting violation')
        for year in protocol['test_years']:
            indices = [i for i, day in enumerate(dates) if day.startswith(str(year))]
            year_return = nav[indices[-1]+1] / nav[indices[0]] - 1
            subset = yearly
            for key in keys:
                subset = subset[subset[key] == row[key]]
            subset = subset[subset.year == year]
            if len(subset) != 1 or not np.isclose(subset.iloc[0].net_return, year_return, atol=1e-10, rtol=0):
                raise ValueError('Yearly result differs from NAV')
        audit.append({'file': path.name, 'sha256': checksum(path), 'metrics_match': True})
    for fold in json.loads((experiment / 'TRAINING_FOLDS.json').read_text()):
        if fold['train_label_last'] >= fold['test_first_open'] or fold['training_dates'] < 126:
            raise ValueError('Test label purge or training history invalid')
        if 'inner_train_label_last' in fold and (fold['inner_train_label_last'] >= fold['validation_signal_first']
                or fold['validation_label_last'] >= fold['test_first_open']):
            raise ValueError('Validation label purge invalid')
    contract = json.loads((experiment / 'EXPERIMENT_CONTRACT.json').read_text())
    for name, expected_hash in contract['result_artifacts'].items():
        if checksum(experiment / name) != expected_hash:
            raise ValueError('Result contract checksum differs')
    return {'status': 'arithmetic_and_artifact_checks_passed', 'verified_at': datetime.now(timezone.utc).isoformat(),
            'simulations': len(table), 'annual_results': len(yearly), 'checks': audit,
            'numerical_target_passes': int(table.numerical_target_pass.sum()),
            'source_quality_passed': False, 'execution_verified': False, 'selection_bias_corrected': False,
            'production_approved': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.experiment)
    dump(args.report, report)
    print(json.dumps({k: v for k, v in report.items() if k != 'checks'}))


if __name__ == '__main__':
    main()
