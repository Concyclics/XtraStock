"""Purged market-risk forecasts from a frozen daily aggregate.

This is a daily-return HAR-style approximation, not intraday HAR-RV. The
target is the next horizon's mean squared close-to-close market return, not a
tradable portfolio return. No full-sample risk normalization or leverage.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from strategy_dataset import checksum, dump

FORECAST_NAMES = ('market_sample20', 'market_ewma20', 'market_har20', 'training_mean20')
DEFAULT_CONFIG = {
    'horizon': 20, 'feature_windows': [1, 5, 22], 'refit': 'quarterly',
    'training_sessions': 756, 'minimum_training_dates': 200,
    'ridge_penalty': 10.0, 'ewma_half_life': 20.0, 'minimum_market_members': 20,
}


def validate_config(config):
    # Names encode the fixed horizon. New model families need a new contract.
    if config != DEFAULT_CONFIG:
        raise ValueError('Unsupported market-risk specification; predeclare a new model contract')


def market_inputs(data, features):
    """Prior eligible membership, no future membership or zero-filled gaps."""
    previous = np.vstack([np.zeros((1, len(data['symbols'])), bool), features['eligible'][:-1]])
    returns = features['returns']
    valid = previous & np.isfinite(returns) & (np.abs(returns) <= .35)
    count = valid.sum(axis=1)
    value = np.divide(np.where(valid, returns, 0).sum(axis=1), count,
                      out=np.full(len(count), np.nan), where=count >= 20)
    return pd.DataFrame({'date': data['dates'], 'market_return': value,
                         'market_members': count, 'market_on': features['market_on'].astype(int)})


def forward_variance(returns, horizon):
    """Complete next-session windows; neither extend expiry nor fill gaps."""
    values = np.asarray(returns, float)
    result = np.full(len(values), np.nan)
    for i in range(len(values) - horizon):
        future = values[i+1:i+horizon+1]
        if np.isfinite(future).all():
            result[i] = float(np.mean(future**2))
    return result


def ewma_variance(returns, half_life=20., minimum=22):
    alpha = 1 - 2**(-1 / half_life)
    out = np.full(len(returns), np.nan)
    state, count = 0., 0
    for i, value in enumerate(returns):
        if not np.isfinite(value):
            state, count = 0., 0
            continue
        state = value**2 if count == 0 else (1-alpha)*state + alpha*value**2
        count += 1
        if count >= minimum:
            out[i] = state
    return out


def forecast(inputs, config, primary_since, evaluation_start, evaluation_end):
    validate_config(config)
    dates = np.asarray(inputs['date'], str)
    if len(dates) < 2 or len(set(dates)) != len(dates) or np.any(dates[1:] <= dates[:-1]):
        raise ValueError('Market-risk calendar must be unique and increasing')
    if primary_since > evaluation_start or evaluation_start > evaluation_end:
        raise ValueError('Invalid market-risk evaluation window')
    counts = np.asarray(inputs['market_members'], float)
    on = np.asarray(inputs['market_on'], float)
    if not np.isfinite(counts).all() or np.any(counts < 0) or np.any(counts != np.floor(counts)) or not np.isin(on, [0, 1]).all():
        raise ValueError('Invalid market membership or trend state')
    returns = np.asarray(inputs['market_return'], float).copy()
    returns[counts < config['minimum_market_members']] = np.nan
    if np.any(np.isfinite(returns) & (np.abs(returns) > .35)) or np.isinf(returns).any():
        raise ValueError('Invalid aggregate market return')
    horizon = config['horizon']
    squared = pd.Series(returns**2)
    x = np.column_stack([np.log(np.maximum(squared.rolling(window, min_periods=window).mean(), 1e-10))
                         for window in config['feature_windows']])
    target = forward_variance(returns, horizon)
    output = {name: np.full(len(dates), np.nan) for name in FORECAST_NAMES}
    output['market_sample20'] = pd.Series(returns).rolling(20, min_periods=20).var(ddof=1).to_numpy()
    output['market_ewma20'] = ewma_variance(returns, config['ewma_half_life'])
    # A forecast is made at the close before its next-open account date.
    opens = np.flatnonzero((dates >= evaluation_start) & (dates <= evaluation_end))
    if not len(opens) or opens[0] == 0:
        raise ValueError('Missing market-risk signal warmup')
    quarter = lambda day: f'{day[:4]}Q{(int(day[5:7])-1)//3+1}'
    periods = sorted({quarter(dates[i]) for i in opens})
    folds = []
    for period in periods:
        oi = np.asarray([i for i in opens if quarter(dates[i]) == period])
        pi = oi - 1
        first = int(pi[0])
        candidates = np.arange(len(dates))
        train = candidates[(dates >= primary_since) & (candidates >= first-config['training_sessions'])
                           & (candidates+horizon < first) & np.isfinite(target)
                           & (target > 0) & np.isfinite(x).all(axis=1)]
        record = {'period': period, 'first_open': str(dates[oi[0]]), 'last_open': str(dates[oi[-1]]),
                  'first_signal': str(dates[first]), 'training_dates': len(train),
                  'train_signal_first': str(dates[train[0]]) if len(train) else None,
                  'train_signal_last': str(dates[train[-1]]) if len(train) else None,
                  'train_label_last': str(dates[train[-1]+horizon]) if len(train) else None,
                  'training_signal_indices': train.tolist(), 'prediction_signal_indices': pi.tolist()}
        if len(train) < config['minimum_training_dates']:
            record['status'] = 'insufficient_matured_history'
            folds.append(record)
            continue
        mean = x[train].mean(axis=0)
        scale = x[train].std(axis=0)
        scale = np.where(scale > 1e-12, scale, 1.)
        z = (x[train]-mean)/scale
        y = np.log(target[train])
        intercept = float(y.mean())
        beta = np.linalg.solve(z.T@z + np.eye(z.shape[1])*config['ridge_penalty'], z.T@(y-intercept))
        residual = y - (intercept+z@beta)
        smearing = float(np.mean(np.exp(residual)))
        low, high = float(y.min()), float(y.max())
        good = pi[np.isfinite(x[pi]).all(axis=1)]
        pred_log = intercept + ((x[good]-mean)/scale)@beta
        output['market_har20'][good] = np.exp(np.clip(pred_log, low, high))*smearing
        output['training_mean20'][pi] = float(target[train].mean())
        record.update(status='fitted', feature_mean=mean.tolist(), feature_scale=scale.tolist(),
                      coefficients=beta.tolist(), intercept=intercept, smearing=smearing,
                      training_log_target_bounds=[low, high], training_mean=float(target[train].mean()),
                      valid_predictions=len(good))
        folds.append(record)
    table = pd.DataFrame({'date': dates, 'realized_variance20': target, **output})
    signal = np.zeros(len(dates), bool)
    signal[opens-1] = True
    table['evaluation_signal'] = signal
    table['evaluation_open'] = ['']*len(table)
    table.loc[opens-1, 'evaluation_open'] = dates[opens]
    return table, folds


def prediction_metrics(table):
    """Common support; overlap and a fixed nonoverlapping calendar separately."""
    signal = np.asarray(table['evaluation_signal'], bool)
    eligible = signal & np.isfinite(table[list(FORECAST_NAMES)]).all(axis=1) & np.isfinite(table['realized_variance20'])
    eligible &= (table[list(FORECAST_NAMES)] > 0).all(axis=1) & (table['realized_variance20'] > 0)
    signal_indices = np.flatnonzero(signal)
    nonoverlap = np.zeros(len(table), bool)
    nonoverlap[signal_indices[::20]] = True
    years = np.asarray(table['evaluation_open'], str)
    result = []
    for sampling, sub in [('all_matured', np.ones(len(table), bool)), ('nonoverlap20', nonoverlap)]:
        for year in ['all', *sorted({day[:4] for day in years[signal]})]:
            use = eligible & sub & (True if year == 'all' else np.char.startswith(years, year))
            if not use.any():
                continue
            actual = table.loc[use, 'realized_variance20'].to_numpy(float)
            sample = table.loc[use, 'market_sample20'].to_numpy(float)
            train_mean = table.loc[use, 'training_mean20'].to_numpy(float)
            mse_sample = np.mean((actual-sample)**2)
            mse_mean = np.mean((actual-train_mean)**2)
            for name in FORECAST_NAMES:
                predicted = table.loc[use, name].to_numpy(float)
                mse = float(np.mean((actual-predicted)**2))
                ratio = actual/predicted
                result.append({'model': name, 'allocation': 'risk_prediction_'+sampling, 'cohort': 'market_aggregate',
                    'year': year, 'horizon': 20, 'observations': int(use.sum()),
                    'variance_mse': mse, 'mse_skill_vs_sample20': 1-mse/mse_sample if mse_sample else None,
                    'mse_skill_vs_training_mean': 1-mse/mse_mean if mse_mean else None,
                    'qlike': float(np.mean(ratio-np.log(ratio)-1)),
                    'annual_volatility_mae': float(np.mean(np.abs(np.sqrt(252*actual)-np.sqrt(252*predicted)))),
                    'risk_underestimate_rate': float(np.mean(predicted<actual)),
                    'fourfold_variance_underestimate_rate': float(np.mean(actual>4*predicted)),
                    'period_start': str(table.loc[use, 'evaluation_open'].iloc[0]),
                    'period_end': str(table.loc[use, 'evaluation_open'].iloc[-1]),
                    'production_approved': False})
    return pd.DataFrame(result)


def build_artifacts(out, inputs, spec, protocol_hash):
    out = Path(out)
    source = out/'MARKET_RISK_INPUT.csv'
    inputs.to_csv(source, index=False)
    # Use exactly the persisted floats in training and independent validation.
    stored = pd.read_csv(source, float_precision='round_trip')
    table, folds = forecast(stored, spec['market_risk_model'], spec['primary_since'],
                            f"{min(spec['test_years'])}-01-01", spec['evaluation_until'])
    table.to_csv(out/'MARKET_RISK_FORECASTS.csv', index=False)
    dump(out/'MARKET_RISK_FOLDS.json', folds)
    prediction_metrics(table).to_csv(out/'RISK_PREDICTION_RESULTS.csv', index=False)
    names = ['MARKET_RISK_INPUT.csv', 'MARKET_RISK_FORECASTS.csv', 'MARKET_RISK_FOLDS.json', 'RISK_PREDICTION_RESULTS.csv']
    dump(out/'MARKET_RISK_RECEIPT.json', {'protocol_sha256': protocol_hash,
        'artifacts': {name: checksum(out/name) for name in names}, 'market_source': spec.get('source', {}),
        'data_contract': 'Prior-eligible all-universe aggregate; daily squared returns, not intraday realized variance.',
        'missing_evaluation_forecasts': int((table['evaluation_signal'] & table['market_har20'].isna()).sum()),
        'pending_outcomes': int((table['evaluation_signal'] & table['realized_variance20'].isna()).sum())})
    return {name: np.sqrt(np.maximum(table[name].to_numpy(float), 0)*252) for name in FORECAST_NAMES}


def verify_artifacts(experiment, spec, protocol_hash):
    """Rebuild every fit and forecast, not just transport hashes or summaries."""
    experiment = Path(experiment)
    receipt = json.loads((experiment/'MARKET_RISK_RECEIPT.json').read_text())
    required = {'MARKET_RISK_INPUT.csv', 'MARKET_RISK_FORECASTS.csv', 'MARKET_RISK_FOLDS.json', 'RISK_PREDICTION_RESULTS.csv'}
    if receipt['protocol_sha256'] != protocol_hash or receipt['market_source'] != spec.get('source', {}) or set(receipt['artifacts']) != required:
        raise ValueError('Market-risk provenance differs')
    for name, digest in receipt['artifacts'].items():
        if checksum(experiment/name) != digest:
            raise ValueError('Market-risk artifact differs: '+name)
    inputs = pd.read_csv(experiment/'MARKET_RISK_INPUT.csv', float_precision='round_trip')
    actual = pd.read_csv(experiment/'MARKET_RISK_FORECASTS.csv', float_precision='round_trip', keep_default_na=True)
    actual['evaluation_open'] = actual['evaluation_open'].fillna('')
    expected, folds = forecast(inputs, spec['market_risk_model'], spec['primary_since'],
        f"{min(spec['test_years'])}-01-01", spec['evaluation_until'])
    try:
        pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    except AssertionError as exc:
        raise ValueError('Market-risk forecast differs from causal reconstruction') from exc
    if folds != json.loads((experiment/'MARKET_RISK_FOLDS.json').read_text()):
        raise ValueError('Market-risk training folds differ')
    # Independent boundary and target checks, including the exact last expiry.
    for fold in folds:
        if fold['training_dates']:
            if fold['train_signal_first'] < spec['primary_since'] or fold['train_label_last'] >= fold['first_signal']:
                raise ValueError('Market-risk label crosses training boundary')
    observed_metrics = pd.read_csv(experiment/'RISK_PREDICTION_RESULTS.csv', float_precision='round_trip')
    try:
        pd.testing.assert_frame_equal(observed_metrics, prediction_metrics(expected), check_exact=True)
    except AssertionError as exc:
        raise ValueError('Market-risk score differs from forecasts') from exc
    if receipt['missing_evaluation_forecasts'] != int((expected['evaluation_signal'] & expected['market_har20'].isna()).sum()) or receipt['pending_outcomes'] != int((expected['evaluation_signal'] & expected['realized_variance20'].isna()).sum()):
        raise ValueError('Market-risk coverage differs')
    return {str(row['date']): {**{name: float(np.sqrt(max(row[name], 0)*252)) for name in FORECAST_NAMES},
            'market_on': bool(inputs.iloc[i]['market_on'])} for i, row in expected.iterrows()}


def verify_risk_budget(result, risks):
    policy = result['policy']
    estimator = policy['vol_estimator']
    if estimator not in ('market_ewma20', 'market_har20'):
        return
    calendar = list(risks)
    positions = {day: i for i, day in enumerate(calendar)}
    expected_dates = [day for i, day in enumerate(result['dates']) if i % policy['rebalance'] == 0
                      or (policy['risk_rebalance'] is not None and i % policy['risk_rebalance'] == 0)]
    if [step['date'] for step in result['rebalance_log']] != expected_dates:
        raise ValueError('Market-risk rebalance records missing or duplicated')
    for step in result['rebalance_log']:
        index = positions[step['date']]
        if index < policy['signal_lag'] or calendar[index-policy['signal_lag']] != step['signal_date']:
            raise ValueError('Market-risk signal is not the previous session')
        row = risks[step['signal_date']]
        vol = row[estimator]
        expected_vol = vol if np.isfinite(vol) else None
        recorded = step['estimated_annual_volatility']
        if (recorded is None) != (expected_vol is None) or (expected_vol is not None and not np.isclose(recorded, expected_vol, atol=1e-12, rtol=0)):
            raise ValueError('Market-risk budget used a different forecast')
        allocation = policy['exposure_floor'] if policy['regime'] == 'lagged_market_trend' and not row['market_on'] else 1.
        allocation *= min(1., policy['target_vol']/max(vol, .05)) if np.isfinite(vol) else 0.
        if not np.isclose(step['target_allocation'], allocation, atol=1e-12, rtol=0):
            raise ValueError('Market-risk allocation differs from prior forecast')
