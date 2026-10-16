"""Independently reconstruct planned weights, trend and covariance exposure.

This checks the target rule at every decision, not a guarantee that blocked
orders achieved the target or that realized drawdown respects a volatility goal.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path

import numpy as np

from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features


def normalized_weights(raw, cap):
    """Sorted saturation solution, separate from the simulator water fill."""
    raw = np.asarray(raw, float)
    if np.any(~np.isfinite(raw)) or np.any(raw <= 0) or not 0 < cap <= 1:
        raise ValueError('Invalid raw stock weights')
    if len(raw)*cap <= 1:
        return np.full(len(raw), cap)
    order = np.argsort(-raw, kind='stable')
    sorted_raw = raw[order]
    remaining = np.cumsum(sorted_raw[::-1])[::-1]
    for saturated in range(len(raw)):
        scale = (1-saturated*cap)/remaining[saturated]
        if scale*sorted_raw[saturated] <= cap+1e-14:
            result = np.empty(len(raw))
            result[order[:saturated]] = cap
            result[order[saturated:]] = scale*sorted_raw[saturated:]
            return result
    raise ValueError('No feasible weight normalization')


def expected_budget(policy, features, signal, columns):
    columns = np.asarray(columns, int)
    raw = np.ones(len(columns))
    if policy['weighting'] == 'inverse_volatility':
        vol = features['vol20'][signal, columns]
        if np.any(~np.isfinite(vol)) or np.any(vol <= 0):
            raise ValueError('Selected inverse-volatility stock lacks a risk observation')
        raw = 1/np.maximum(vol, .005)
    elif policy['weighting'] != 'equal':
        raise ValueError('Unsupported stock weighting')
    weights = normalized_weights(raw, policy['max_weight'])
    allocation = policy['exposure_floor'] if policy['regime'] == 'lagged_market_trend' and not features['market_on'][signal] else 1.
    vol, observations = None, None
    if policy['target_vol'] is not None:
        if policy['vol_estimator'] != 'portfolio':
            raise ValueError('This independent audit requires portfolio covariance risk')
        history = features['returns'][max(0, signal-policy['vol_window']+1):signal+1, columns]
        history = history[np.isfinite(history).all(axis=1)]
        observations = len(history) if len(columns) else 0
        if observations >= policy['vol_min_observations']:
            weighted = (history-history.mean(axis=0))*weights
            basket = weighted.sum(axis=1)
            sample = float(basket@basket)/(observations-1)
            diagonal = float(np.sum(weighted**2))/(observations-1)
            shrink = policy['covariance_shrinkage']
            vol = float(np.sqrt(max(0., (1-shrink)*sample+shrink*diagonal)*252))
            allocation *= min(1., policy['target_vol']/max(vol, .05))
        else:
            allocation = 0.
    return allocation, vol, observations


def audit_account(result, data, features, cache=None):
    cache = {} if cache is None else cache
    policy = result['policy']
    dates = list(result['dates'])
    date_index = {str(value): i for i, value in enumerate(data['dates'])}
    symbol_index = {str(value): i for i, value in enumerate(data['symbols'])}
    expected_days = [day for i, day in enumerate(dates) if i % policy['rebalance'] == 0 or
                     (policy['risk_rebalance'] is not None and i % policy['risk_rebalance'] == 0)]
    if [step['date'] for step in result['rebalance_log']] != expected_days:
        raise ValueError('Target decisions differ from declared selection/refresh schedule')
    decisions, missing, max_error = 0, 0, 0.
    for step in result['rebalance_log']:
        signal = date_index[step['date']]-policy['signal_lag']
        if (signal < 0 or step['signal_date'] != str(data['dates'][signal]) or policy['signal_lag'] != 1
                or bool(step['selection_due']) != (dates.index(step['date']) % policy['rebalance'] == 0)):
            raise ValueError('Risk or selection decision used the wrong signal date')
        columns = tuple(symbol_index[symbol] for symbol in step['selected_symbols'])
        keys = ('weighting','max_weight','regime','exposure_floor','target_vol','vol_estimator',
                'vol_window','vol_min_observations','covariance_shrinkage')
        key = (signal, columns, *(policy[k] for k in keys))
        if key not in cache:
            cache[key] = expected_budget(policy, features, signal, columns)
        allocation, volatility, observations = cache[key]
        if step['risk_observations'] != observations or step['vol_estimator'] != policy['vol_estimator']:
            raise ValueError('Risk-history coverage or estimator differs')
        actual = step['estimated_annual_volatility']
        if (actual is None) != (volatility is None):
            raise ValueError('Missing risk history was silently substituted')
        if volatility is not None:
            error = abs(actual-volatility)
            max_error = max(max_error, error)
            if error > 1e-10:
                raise ValueError('Covariance estimate differs from selected stock weights')
        if not np.isclose(step['target_allocation'], allocation, atol=1e-10, rtol=0):
            raise ValueError('Target exposure differs from weights, trend and volatility rules')
        missing += policy['target_vol'] is not None and volatility is None
        decisions += 1
    return {'decisions': decisions, 'missing_risk_decisions': int(missing),
            'maximum_volatility_difference': max_error, 'target_rules_match': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('experiment','dataset','report'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads((args.experiment/'PROTOCOL.json').read_text())
    done = json.loads((args.experiment/'COMPLETE.json').read_text())
    if done['protocol_sha256'] != checksum(args.experiment/'PROTOCOL.json') or not spec.get('exposure_audit'):
        raise ValueError('Exposure audit was not declared in completed protocol')
    for name, expected in spec['source'].items():
        if checksum(args.dataset/name) != expected:
            raise ValueError('Risk-audit market differs from frozen inputs')
    name = Path(__file__).name
    if checksum(Path(__file__)) != spec['code'][name] or checksum(args.experiment/'code-at-execution'/name) != spec['code'][name]:
        raise ValueError('Exposure verifier differs from frozen code')
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    cache, rows = {}, []
    for path in sorted((args.experiment/'simulations').glob('*.json.gz')):
        with gzip.open(path, 'rt') as stream:
            result = json.load(stream)
        if result['policy']['family'] not in spec['models']:
            continue
        if result['protocol_sha256'] != done['protocol_sha256']:
            raise ValueError('Account belongs to a different protocol')
        rows.append({'account': path.name, 'sha256': checksum(path), **audit_account(result, data, features, cache)})
    expected = len(spec['models'])*len(spec['allocations'])*len(spec['cohorts'])*len(spec['cost_bps_per_side'])
    if len(rows) != expected:
        raise ValueError('Missing or additional model accounts')
    report = {'status': 'planned_exposure_verified', 'at': datetime.now(timezone.utc).isoformat(),
        'accounts': len(rows), 'decisions': sum(row['decisions'] for row in rows), 'unique_budget_calculations': len(cache),
        'maximum_volatility_difference': max(row['maximum_volatility_difference'] for row in rows),
        'checks': rows, 'production_approved': False,
        'scope': 'Planned weights, risk history and target allocation; actual fills and realized drawdown are separate.'}
    dump(args.report, report)
    print(json.dumps({k: v for k, v in report.items() if k != 'checks'}))


if __name__ == '__main__':
    main()
