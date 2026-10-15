"""Run a predeclared, resumable research cycle; never approve a trading model."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import itertools
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from strategy_dataset import checksum, dump
from strategy_research import Policy, load_dataset, prepare_features, backtest, paired_comparison
from quant_learning import MODEL_NAMES, research_features, walk_forward, block_bootstrap_cagr, evaluation_window

ALLOCATIONS = {
    'equal_full': {},
    'inverse_full': {'weighting': 'inverse_volatility'},
    'inverse_risk': {'weighting': 'inverse_volatility', 'target_vol': .18,
                     'regime': 'lagged_market_trend', 'exposure_floor': .35, 'risk_rebalance': 5},
}


def protocol(dataset, forecasts):
    code = Path(__file__).parent
    dataset_manifest = json.loads((dataset / 'manifest.json').read_text())
    policy_path = code.parents[3] / 'config/research/policy.json'
    research_policy = json.loads(policy_path.read_text())
    if research_policy['training_lookback_years'] != 3:
        raise ValueError('This experiment currently requires a three-year training window')
    with np.load(dataset / 'market.npz', allow_pickle=False) as panel:
        window = evaluation_window(dataset_manifest, panel['dates'], research_policy['feature_warmup_sessions'])
    return {
        'version': 'quant-cycle-v3',
        'research_policy': research_policy,
        'research_policy_sha256': checksum(policy_path),
        'primary_since': dataset_manifest.get('primary_since'),
        'source': {name: checksum(dataset / name) for name in ('market.npz', 'manifest.json', 'assets.json', 'actions.json')},
        'forecast_sha256': checksum(forecasts),
        'code': {name: checksum(code / name) for name in
                 ('run_quant_cycle.py', 'quant_learning.py', 'strategy_research.py', 'strategy_dataset.py')},
        **window, 'horizon': 20, 'models': list(MODEL_NAMES),
        'allocations': ALLOCATIONS, 'holdings': 40, 'holding_buffer': 1.5, 'max_weight': .05,
        'cost_bps_per_side': research_policy['cost_bps_per_side'], 'cohorts': ['discovery', 'cross_section_reserve'],
        'comparisons': 12, 'alpha': 1000, 'training_years': 3, 'minimum_training_dates': 126,
        'selection': 'All 12 predeclared combinations reported; no test-return-based selection or promotion.',
        'accounting': 'Continuous cash/share account across years, next-open execution, ACT/365.25 CAGR. Partial-year returns are labeled and excluded from the positive-complete-years gate.',
        'capital': 10000000, 'leverage': 1, 'cash_interest': 0,
        'scope': 'Historical reconstruction; previously inspected years are exploratory, not untouched holdout.',
        'source_gaps': ['Incomplete historical ST/suspension/delisting and auction liquidity.',
                        'Corporate-action revisions and current security master require independent verification.'],
        'primary_objective': {'net_cagr_above': research_policy['target_net_calendar_cagr'],
                             'max_drawdown_at_most': research_policy['maximum_drawdown'],
                             'period_win_rate_at_least': research_policy['minimum_period_win_rate'],
                             'positive_years_at_least': 3},
        'production_approved': False,
        'research_sources': ['https://www.nber.org/papers/w25398', 'https://www.nber.org/papers/w22208',
                             'https://www.aqr.com/insights/research/working-paper/trading-costs-of-asset-pricing-anomalies',
                             'https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--forecasts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'COMPLETE.json').exists():
        raise SystemExit('Completed output is immutable; choose a new experiment directory')
    spec = protocol(args.dataset, args.forecasts)
    protocol_file = out / 'PROTOCOL.json'
    if protocol_file.exists():
        if json.loads(protocol_file.read_text()) != spec:
            raise SystemExit('Input, code or protocol changed; cannot resume this experiment')
    else:
        dump(protocol_file, spec)
    protocol_hash = checksum(protocol_file)
    frozen_code = out / 'code-at-execution'
    frozen_code.mkdir(exist_ok=True)
    for name, expected in spec['code'].items():
        (frozen_code / name).write_bytes((Path(__file__).parent / name).read_bytes())
        if checksum(frozen_code / name) != expected:
            raise ValueError('Source changed while freezing code')
    started = datetime.now(timezone.utc).isoformat()
    dump(out / 'RUNNING.json', {'started_at': started, 'protocol_sha256': protocol_hash})
    print(json.dumps({'phase': 'protocol_frozen', 'comparisons': 12, 'replays': 56}), flush=True)
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    with np.load(args.forecasts, allow_pickle=False) as source:
        if not np.array_equal(source['dates'], data['dates']) or not np.array_equal(source['symbols'], data['symbols']):
            raise ValueError('Forecast dates/securities do not match market panel')
        forecasts = {k: source[k] for k in ('value', 'revision')}
    dump(out / 'DATA_QUALITY.json', {
        'dates': len(data['dates']), 'candidate_securities': len(data['symbols']),
        'securities_with_prices': int(np.isfinite(data['close']).any(axis=0).sum()),
        'eligible_asset_days': int(features['eligible'].sum()),
        'forecast_value_asset_days': int((features['eligible'] & np.isfinite(forecasts['value'])).sum()),
        'forecast_revision_asset_days': int((features['eligible'] & np.isfinite(forecasts['revision'])).sum()),
        'signal_anomalies': len(features['anomalies']),
        'missing_forecasts_exclude_security': False,
        'historical_lifecycle_complete': False, 'source_quality': 'historical_reconstruction',
    })
    predfile = out / 'PREDICTIONS.npz'
    if predfile.exists():
        receipt = json.loads((out / 'PREDICTION_RECEIPT.json').read_text())
        if receipt['sha256'] != checksum(predfile) or receipt['protocol_sha256'] != protocol_hash:
            raise ValueError('Prediction checkpoint verification failed')
        with np.load(predfile, allow_pickle=False) as z:
            scores = {key: z[key] for key in z.files}
    else:
        matrices = research_features(data, features, forecasts)
        del forecasts
        folds = []
        def checkpoint(fold):
            folds.append(fold)
            dump(out / 'TRAINING_FOLDS.json', folds)
            print(json.dumps({key: fold[key] for key in
                              ('year', 'training_rows', 'training_dates', 'train_label_last', 'test_first_open')}), flush=True)
        scores, _ = walk_forward(data, features, matrices, spec['test_years'], callback=checkpoint)
        np.savez_compressed(predfile, **scores)
        dump(out / 'PREDICTION_RECEIPT.json', {'sha256': checksum(predfile), 'protocol_sha256': protocol_hash})
        del matrices
    evaluate_scores(data, features, scores, spec, out, started, protocol_hash)


def evaluate_scores(data, features, scores, spec, out, started, protocol_hash):
    """Identical execution/cost/reporting contract for every model family."""
    summaries = []
    annual = []
    allocations = spec['allocations']
    total = (2 + len(spec['models']) * len(allocations)) * len(spec['cohorts']) * len(spec['cost_bps_per_side'])
    simulations = out / 'simulations'
    simulations.mkdir(exist_ok=True)
    start = f"{min(spec['test_years'])}-01-01"
    end = spec.get('evaluation_until', f"{max(spec['test_years'])}-12-31")
    complete_years = {str(year) for year in spec.get('complete_test_years', spec['test_years'])}
    capital = float(spec.get('capital', 10_000_000))
    if not np.isfinite(capital) or capital <= 0:
        raise ValueError('capital must be positive and finite')
    for allocation in allocations.values():
        if set(allocation) & {'family', 'cost_bps', 'initial_cash'}:
            raise ValueError('allocation cannot override model, declared cost or capital')
    for cohort, cost in itertools.product(spec['cohorts'], spec['cost_bps_per_side']):
        mask = np.ones(len(data['symbols']), bool) if cohort == 'all' else data['cohort'] == cohort
        if not mask.any():
            raise ValueError('unknown or empty evaluation cohort: ' + cohort)
        scope_features = dict(features)
        equal_score = np.where(features['eligible'] & mask[None, :], 1., np.nan)
        plans = [('equal_weight', 'benchmark', Policy('equal_weight', size=int(mask.sum()),
                  cost_bps=cost, initial_cash=capital, round_lots=False), equal_score)]
        plans.append(('momentum_low_vol', 'benchmark', Policy('momentum_low_vol', size=40,
                      cost_bps=cost, initial_cash=capital, max_weight=.05, holding_buffer=1.5),
                      np.where(mask[None, :], features['score']['momentum_low_vol'], np.nan)))
        for name, allocation in itertools.product(spec['models'], allocations):
            policy_args = {'size': spec.get('holdings', 40), 'max_weight': spec.get('max_weight', .05),
                           'holding_buffer': spec.get('holding_buffer', 1.5), **allocations[allocation]}
            plans.append((name, allocation, Policy(name, cost_bps=cost, initial_cash=capital, **policy_args),
                          np.where(mask[None, :], scores[name], np.nan)))
        baseline = None
        for name, allocation, policy, score in plans:
            begin = time.monotonic()
            file = simulations / f'{cohort}-cost{cost}-{name}-{allocation}.json.gz'
            if file.exists():
                with gzip.open(file, 'rt') as stream:
                    result = json.load(stream)
                if result.get('protocol_sha256') != protocol_hash or result['policy'] != asdict(policy):
                    raise ValueError('Simulation checkpoint mismatch')
            else:
                scope_features['score'] = {name: score}
                result = backtest(data, scope_features, policy, start, end, keep_trades=True)
                result['protocol_sha256'] = protocol_hash
                temp = file.with_suffix('.tmp')
                with gzip.open(temp, 'wt') as stream:
                    json.dump(result, stream, allow_nan=False)
                temp.replace(file)
            if name == 'equal_weight':
                baseline = result
            comparison = paired_comparison(result, baseline)
            metric = result['metrics']
            row = {'model': name, 'allocation': allocation, 'cohort': cohort, 'cost': cost, 'horizon': 20,
                   'initial_cash': policy.initial_cash, 'holdings': policy.size,
                   'year': f"{min(spec['test_years'])}-{max(spec['test_years'])}", 'net_return': metric['net_return'], 'net_cagr': metric['cagr'],
                   'maximum_drawdown': metric['maximum_drawdown'], 'period_win_rate': metric['period_win_rate'],
                   'closed_trade_win_rate': metric['closed_trade_win_rate'], 'closed_trades': metric['closed_trade_count'],
                   'average_exposure': metric['average_exposure'], 'sharpe': metric['sharpe_zero_cash'],
                   'cagr_excess': comparison['cagr_excess'], 'win_rate_difference': comparison['period_win_rate_difference'],
                   'unverified_action_held': metric['blocked']['unverified_action_held'],
                   'ex_post_capacity_breach': metric['blocked']['ex_post_capacity_breach'],
                   'ex_post_no_volume_fill': metric['blocked']['ex_post_no_volume_fill'],
                   'terminal_stale_value': metric['terminal_stale_value'],
                   'positive_years': sum(r['return'] > 0 for year, r in metric['annual'].items() if year in complete_years),
                   'positive_partial_years': sum(r['return'] > 0 for year, r in metric['annual'].items() if year not in complete_years),
                   'complete_evaluation_years': len(complete_years),
                   'evaluation_start': result['dates'][0], 'evaluation_end': result['dates'][-1],
                   'production_approved': False}
            goal = spec['primary_objective']
            row['numerical_target_pass'] = (row['net_cagr'] > goal['net_cagr_above'] and row['maximum_drawdown'] <= goal['max_drawdown_at_most']
                                             and (row['period_win_rate'] or 0) >= goal['period_win_rate_at_least'] and row['positive_years'] >= goal['positive_years_at_least']
                                             and row['cagr_excess'] > 0 and (row['win_rate_difference'] or 0) > 0)
            summaries.append(row)
            for year, value in metric['annual'].items():
                indices = np.flatnonzero(np.char.startswith(np.asarray(result['dates']), year))
                nav = np.asarray(result['nav'])[np.r_[indices[0], indices+1]]
                annual.append({'model': name, 'allocation': allocation, 'cohort': cohort, 'cost': cost,
                               'initial_cash': policy.initial_cash, 'holdings': policy.size,
                               'year': year, 'horizon': 20, 'net_return': value['return'],
                               'maximum_drawdown': float(-np.min(nav / np.maximum.accumulate(nav)-1)),
                               'sessions': value['sessions'], 'is_partial_year': year not in complete_years,
                               'period_start': result['dates'][int(indices[0])],
                               'period_end': result['dates'][int(indices[-1])]})
            pd.DataFrame(summaries).to_csv(out / 'COSTED_RESULTS.csv', index=False)
            pd.DataFrame(annual).to_csv(out / 'YEARLY_RESULTS.csv', index=False)
            print(json.dumps({'phase': 'replay', 'completed': len(summaries), 'of': total,
                              'cohort': cohort, 'cost': cost, 'model': name, 'allocation': allocation,
                              'cagr': row['net_cagr'], 'max_drawdown': row['maximum_drawdown'],
                              'seconds': round(time.monotonic()-begin, 2)}), flush=True)
    # Intervals are reported for every fixed candidate, without selecting a winner.
    intervals = []
    for row in summaries:
        if row['cohort'] != 'cross_section_reserve' or row['cost'] != 15:
            continue
        file = simulations / f"{row['cohort']}-cost15-{row['model']}-{row['allocation']}.json.gz"
        with gzip.open(file, 'rt') as stream:
            result = json.load(stream)
        intervals.append({'model': row['model'], 'allocation': row['allocation'],
                          **block_bootstrap_cagr(result['nav'],result['dates'])})
    dump(out / 'UNCERTAINTY.json', intervals)
    dump(out / 'EXPERIMENT_CONTRACT.json', {
        'contract_version': '1', 'gate_status': 'research_only_execution_and_independent_history_gaps',
        'result_artifacts': {name: checksum(out/name) for name in ('COSTED_RESULTS.csv', 'YEARLY_RESULTS.csv')},
        'metric_units': {**{key: 'fraction' for key in ('net_return', 'net_cagr', 'maximum_drawdown', 'period_win_rate',
                                                     'closed_trade_win_rate', 'cagr_excess', 'win_rate_difference')},
                         'initial_cash': 'CNY', 'holdings': 'securities', 'cost': 'bps_per_side'},
        'comparison_dimensions': ['model', 'allocation', 'cohort', 'cost', 'year', 'initial_cash', 'holdings'],
        'methodology_notes': [spec['accounting'], spec['scope'], 'Cost is basis points on each side.',
                              f"{len(spec['models']) * len(allocations)} fixed candidates plus two benchmarks; all results retained.",
                              'Numerical target pass does not resolve execution/source quality.'],
        'training_status_by_year': {str(y): 'purged_previous_years_discovery_only' for y in spec['test_years']},
    })
    dump(out / 'COMPLETE.json', {
        'status': 'research_cycle_complete', 'started_at': started,
        'completed_at': datetime.now(timezone.utc).isoformat(), 'replays': len(summaries),
        'numerical_passes': sum(row['numerical_target_pass'] for row in summaries),
        'protocol_sha256': protocol_hash, 'prediction_receipt': json.loads((out/'PREDICTION_RECEIPT.json').read_text()),
        'production_approved': False,
    })


if __name__ == '__main__':
    main()
