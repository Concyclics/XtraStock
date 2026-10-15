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
    audit, recomputed = [], []
    complete_years = set(protocol.get('complete_test_years', protocol['test_years']))
    cohort_members = None
    if protocol.get('portfolio_evaluation_contract') == 1:
        member_file = experiment / 'COHORT_MEMBERS.json'
        if checksum(member_file) != protocol['cohort_members_sha256']:
            raise ValueError('Evaluation cohort membership differs from protocol')
        cohort_members = {key: set(value) for key, value in json.loads(member_file.read_text()).items()}
        if 'study_config_sha256' in protocol:
            for name, key in [('STUDY_CONFIG.json', 'study_config_sha256'),
                              ('REFERENCE_PROTOCOL.json', 'reference_protocol_sha256'),
                              ('TRAINING_FOLDS.json', 'reference_training_folds_sha256')]:
                if checksum(experiment / name) != protocol[key]:
                    raise ValueError('Frozen portfolio reference differs: ' + name)
            reference = json.loads((experiment / 'REFERENCE_PROTOCOL.json').read_text())
            if reference['source'] != protocol['source'] or receipt['sha256'] != protocol['reference_predictions_sha256']:
                raise ValueError('Portfolio comparison did not preserve reference inputs and predictions')
            study = json.loads((experiment / 'STUDY_CONFIG.json').read_text())
            if (protocol['capital'] not in study['capital_levels']
                    or any(protocol[key] != study[key] for key in ('models', 'cohorts', 'allocations'))):
                raise ValueError('Portfolio parameters differ from the predeclared study')
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
        if cohort_members is not None:
            capital = protocol['capital']
            policy = result['policy']
            if policy['initial_cash'] != capital or nav[0] != capital or row['initial_cash'] != capital:
                raise ValueError('Actual and reported capital differ from protocol')
            if policy['cost_bps'] != row['cost']:
                raise ValueError('Actual cost differs from reported cost')
            if row['allocation'] == 'benchmark':
                holdings = len(cohort_members[row['cohort']]) if row['model'] == 'equal_weight' else 40
            else:
                holdings = protocol['allocations'][row['allocation']].get('size', protocol.get('holdings', 40))
            if policy['size'] != holdings or row['holdings'] != holdings:
                raise ValueError('Actual and reported holdings differ from protocol')
            allowed = cohort_members[row['cohort']]
            if any(not set(step['selected_symbols']) <= allowed or len(step['selected_symbols']) > holdings
                   for step in result['rebalance_log']):
                raise ValueError('Selected portfolio differs from declared cohort or holdings')
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
        year_returns = {}
        for year in protocol['test_years']:
            indices = [i for i, day in enumerate(dates) if day.startswith(str(year))]
            if not indices:
                raise ValueError('Requested evaluation year has no scored sessions')
            year_return = nav[indices[-1]+1] / nav[indices[0]] - 1
            year_returns[year] = year_return
            subset = yearly
            for key in keys:
                subset = subset[subset[key] == row[key]]
            subset = subset[subset.year == year]
            if len(subset) != 1 or not np.isclose(subset.iloc[0].net_return, year_return, atol=1e-10, rtol=0):
                raise ValueError('Yearly result differs from NAV')
            if cohort_members is not None and (
                    subset.iloc[0].initial_cash != capital or subset.iloc[0].holdings != holdings):
                raise ValueError('Yearly capital or holdings differ from actual policy')
            if 'complete_test_years' in protocol:
                record = subset.iloc[0]
                if (bool(record.is_partial_year) != (year not in complete_years)
                        or record.period_start != dates[indices[0]] or record.period_end != dates[indices[-1]]):
                    raise ValueError('Full-year/partial-year reporting differs from the scored calendar')
        positive_years = sum(value > 0 for year, value in year_returns.items() if year in complete_years)
        if row['positive_years'] != positive_years:
            raise ValueError('Positive full-year count differs from NAV')
        if 'complete_test_years' in protocol and (
                row['evaluation_start'] != dates[0] or row['evaluation_end'] != dates[-1]
                or row['complete_evaluation_years'] != len(complete_years)
                or row['positive_partial_years'] != sum(value > 0 for year, value in year_returns.items() if year not in complete_years)):
            raise ValueError('Evaluation interval or partial-year count differs')
        if protocol.get('evaluation_until') and dates[-1] != protocol['evaluation_until']:
            raise ValueError('Evaluation stopped before the declared final session')
        recomputed.append((row, checks, positive_years))
        audit.append({'file': path.name, 'sha256': checksum(path), 'metrics_match': True})
    baselines = {(row['cohort'], row['cost']): checks for row, checks, _ in recomputed
                 if row['model'] == 'equal_weight' and row['allocation'] == 'benchmark'}
    goal = protocol['primary_objective']
    for row, checks, positives in recomputed:
        baseline = baselines[(row['cohort'], row['cost'])]
        excess = checks['net_cagr'] - baseline['net_cagr']
        win_difference = checks['period_win_rate'] - baseline['period_win_rate']
        if not np.isclose(row['cagr_excess'], excess, atol=1e-10, rtol=0) or not np.isclose(row['win_rate_difference'], win_difference, atol=1e-10, rtol=0):
            raise ValueError('Reported baseline advantage differs from independently recomputed NAV')
        passed = (checks['net_cagr'] > goal['net_cagr_above']
                  and checks['maximum_drawdown'] <= goal['max_drawdown_at_most']
                  and checks['period_win_rate'] >= goal['period_win_rate_at_least']
                  and positives >= goal['positive_years_at_least'] and excess > 0 and win_difference > 0)
        if bool(row['numerical_target_pass']) != bool(passed):
            raise ValueError('Numerical target gate differs from independently recomputed NAV')
    for fold in json.loads((experiment / 'TRAINING_FOLDS.json').read_text()):
        if protocol.get('primary_since') and fold['train_signal_first'] < protocol['primary_since']:
            raise ValueError('Training used feature warm-up rows outside the primary research window')
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
