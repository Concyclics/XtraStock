"""Bounded, date-purged stock ranking experiments on immutable local panels.

Models estimate relative 20-session returns, not an absolute profit promise.
All transforms fit on the training fold. No GPU, network or database writes.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategy_research import lag, rank, rolling

MODEL_NAMES = ('ridge_price', 'ridge_interactions', 'ridge_fundamentals', 'ridge_blend')
PRICE_NAMES = ('r5', 'r20', 'r60', 'r120', 'vol20', 'vol60', 'drawdown20',
               'log_volume_ratio', 'log_adv', 'relative_r20', 'market_r20', 'market_vol20', 'trend120')


def evaluation_window(manifest, dates, warmup_sessions=252):
    """Choose calendar folds before reading returns, with two purged splits.

    The shared ridge/tree comparison needs 126 inner training sessions, 63
    validation sessions and two 20-session label gaps. Actual usable labels
    are checked again at fitting time; calendar feasibility is not data quality.
    """
    dates = np.asarray(dates)
    if warmup_sessions < 0 or len(dates) <= warmup_sessions:
        raise ValueError('insufficient calendar for feature warm-up')
    if not (dates[1:] > dates[:-1]).all():
        raise ValueError('calendar must be unique and increasing')
    primary = max(str(manifest.get('primary_since') or dates[0]), str(dates[warmup_sessions]))
    until = str(manifest.get('until') or dates[-1])
    if dates[-1] > until:
        raise ValueError('market calendar exceeds its manifest boundary')
    years, skipped = [], []
    for year in sorted({int(day[:4]) for day in dates if day >= primary}):
        permitted, first = purged_training_dates(dates, year, 20, primary)
        days = np.flatnonzero(permitted)
        if len(days) < 63 or np.count_nonzero(days + 20 < days[-63]) < 126:
            skipped.append(year)
        else:
            years.append(year)
    if not years:
        raise ValueError('no calendar year has sufficient purged training history')
    complete = [year for year in years if primary <= f'{year}-01-01' and until >= f'{year}-12-31']
    return {'test_years': years, 'complete_test_years': complete,
            'partial_test_years': [year for year in years if year not in complete],
            'calendar_only_skipped_years': skipped,
            'evaluation_until': min(until, str(dates[-1]))}


def price_features(data, features):
    adj = np.where((data['adjustment_quality'] > 0) & (data['adjustment_quality'] < 3),
                   data['close'] * data['total_return_factor'], np.nan)
    with np.errstate(divide='ignore', invalid='ignore'):
        returns = [adj / lag(adj, horizon) - 1 for horizon in (5, 20, 60, 120)]
        drawdown = adj / rolling(adj, 20, 'max', 15) - 1
        volume_ratio = data['volume'] / rolling(data['volume'], 20, 'mean', 15)
    # Today's cross section is public at today's close. Eligibility is lagged
    # when computing the market return, so today's movers do not select it.
    previous = np.vstack([np.zeros((1, adj.shape[1]), bool), features['eligible'][:-1]])
    valid = previous & np.isfinite(features['returns']) & (np.abs(features['returns']) <= .35)
    count = valid.sum(axis=1)
    market_return = np.divide(np.where(valid, features['returns'], 0).sum(axis=1), count,
                              out=np.zeros(len(count)), where=count > 0)
    index = np.cumprod(1 + market_return)
    market20 = index / lag(index, 20) - 1
    volatility = rolling(market_return[:, None], 20, 'std', 15)[:, 0]
    broadcast = lambda array: np.broadcast_to(array[:, None], adj.shape)
    return np.stack([*returns, features['vol20'], rolling(features['returns'], 60, 'std', 45),
                     drawdown, np.log(np.clip(volume_ratio, .05, 20)),
                     np.log(np.maximum(features['adv'], 1)), returns[1] - broadcast(market20),
                     broadcast(market20), broadcast(volatility),
                     broadcast(features['market_on'].astype(float))], axis=-1).astype(np.float32)


def research_features(data, features, forecasts=None):
    price = price_features(data, features)
    eligible = features['eligible']
    # Cross-sectional ranks reduce unit sensitivity and extreme-value leverage.
    ranked = np.stack([2 * rank(price[:, :, j], eligible) - 1 for j in range(10)], axis=-1)
    liquidity = ranked[:, :, 8]
    vol = ranked[:, :, 4]
    trend = 2 * features['market_on'][:, None] - 1
    interactions = np.stack([
        ranked[:, :, 0] * vol, ranked[:, :, 0] * liquidity,
        ranked[:, :, 2] * vol, ranked[:, :, 3] * liquidity,
        ranked[:, :, 1] * trend, ranked[:, :, 4] * trend,
    ], axis=-1)
    transformed = np.concatenate([ranked, price[:, :, 10:], interactions], axis=-1).astype(np.float32)
    result = {'ridge_price': price, 'ridge_interactions': transformed}
    if forecasts is not None:
        extra = []
        for key in ('value', 'revision'):
            available = np.isfinite(forecasts[key]) & eligible
            # Neutral rank plus an explicit availability indicator; an absent
            # EPS estimate is not recorded or treated as a genuine zero EPS.
            value = 2 * rank(forecasts[key], available) - 1
            extra.extend([np.where(available, value, 0), available.astype(float)])
        fundamentals = np.stack(extra, axis=-1).astype(np.float32)
        result['ridge_fundamentals'] = np.concatenate([transformed, fundamentals], axis=-1)
    return result


def labels(data, features, horizon=20):
    """Exact market-calendar expiry; no extending labels across missing bars."""
    adjusted = data['close'] * data['total_return_factor']
    opened = data['open'] * data['total_return_factor']
    valid = (features['valid'] & (data['volume'] > 0) & np.isfinite(adjusted + opened)
             & (data['adjustment_quality'] > 0) & (data['adjustment_quality'] < 3))
    cumulative = np.vstack([np.zeros((1, adjusted.shape[1]), int), np.cumsum(valid, axis=0)])
    outcome = np.full_like(adjusted, np.nan)
    for i in range(len(adjusted) - horizon):
        ok = (cumulative[i + horizon + 1] - cumulative[i + 1] == horizon) & features['eligible'][i]
        outcome[i, ok] = adjusted[i + horizon, ok] / opened[i + 1, ok] - 1
    # Reserve-stock outcome labels never enter the training benchmark either.
    discovery = data['cohort'] == 'discovery'
    observed = np.isfinite(outcome[:, discovery]).sum(axis=1)
    mean = np.divide(np.nansum(outcome[:, discovery], axis=1), observed,
                     out=np.full(len(outcome), np.nan), where=observed > 0)
    return outcome, outcome - mean[:, None]


def fit_ridge(x, y, signal_indices, alpha=1000.0):
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    if len(x) < 1000 or len(np.unique(signal_indices)) < 126:
        raise ValueError('insufficient matured training observations')
    if not np.all(np.isfinite(y)) or alpha <= 0:
        raise ValueError('invalid target or regularization')
    medians = np.array([np.median(col[np.isfinite(col)]) if np.isfinite(col).any() else 0
                        for col in x.T])
    filled = np.where(np.isfinite(x), x, medians)
    mean = filled.mean(axis=0)
    scale = filled.std(axis=0)
    scale = np.where(scale > 1e-8, scale, 1.)
    transformed = np.clip((filled - mean) / scale, -8, 8)
    design = np.column_stack([np.ones(len(x)), transformed])
    _, inverse, counts = np.unique(signal_indices, return_inverse=True, return_counts=True)
    weight = 1. / counts[inverse]
    weight /= weight.mean()
    penalty = np.eye(design.shape[1]) * alpha
    penalty[0, 0] = 0.
    coefficient = np.linalg.solve(design.T @ (weight[:, None] * design) + penalty,
                                  design.T @ (weight * np.clip(y, -.5, .5)))
    return {'median': medians, 'mean': mean, 'scale': scale, 'coefficient': coefficient,
            'training_rows': len(y), 'training_dates': len(counts), 'alpha': alpha}


def predict(model, x):
    filled = np.where(np.isfinite(x), x, model['median'])
    transformed = np.clip((filled - model['mean']) / model['scale'], -8, 8)
    return model['coefficient'][0] + transformed @ model['coefficient'][1:]


def purged_training_dates(dates, test_year, horizon=20, primary_since=None):
    """Respect the primary research window while retaining older feature inputs."""
    if horizon < 1:
        raise ValueError('label horizon must be positive')
    first = int(np.searchsorted(dates, f'{test_year}-01-01'))
    if first >= len(dates) or not str(dates[first]).startswith(str(test_year) + '-'):
        raise ValueError('test year has no sessions')
    lower = max(f'{test_year - 3}-01-01', primary_since or '')
    return ((dates >= lower) & (np.arange(len(dates)) + horizon < first)), first


def walk_forward(data, features, matrices, years, horizon=20, callback=None):
    dates = data['dates']
    _, target = labels(data, features, horizon)
    positions = np.arange(len(dates))[:, None]
    discovery = data['cohort'][None, :] == 'discovery'
    # All signal dates are used; sample weights give each day equal influence.
    outputs = {name: np.full_like(data['close'], np.nan) for name in MODEL_NAMES}
    folds = []
    for year in years:
        permitted, first = purged_training_dates(dates, year, horizon, data.get('primary_since'))
        train = (features['eligible'] & discovery & np.isfinite(target)
                 & permitted[:, None])
        # First signal is the previous year's final close, for January's open.
        test_days = (positions >= first - 1) & (dates[:, None] < f'{year+1}-01-01')
        test = features['eligible'] & test_days
        ti, tj = np.where(train)
        pi, pj = np.where(test)
        if not len(ti) or ti.max() + horizon >= first or not len(pi):
            raise ValueError('invalid purged year split')
        # A next-year model must not overwrite the final signal used for an
        # earlier year's already-executed orders. The overwrite below is only
        # the Dec final-close signal, whose trade is in the new test year.
        record = {'year': year, 'train_signal_first': str(dates[ti.min()]),
                  'train_label_last': str(dates[ti.max()+horizon]),
                  'test_first_open': str(dates[first]), 'training_rows': len(ti),
                  'training_dates': len(np.unique(ti)), 'test_predictions': len(pi), 'models': {}}
        for name in ('ridge_price', 'ridge_interactions', 'ridge_fundamentals'):
            if name not in matrices:
                raise ValueError(f'missing required feature family: {name}')
            model = fit_ridge(matrices[name][ti, tj], target[ti, tj], ti)
            for start in range(0, len(pi), 100000):
                stop = start + 100000
                outputs[name][pi[start:stop], pj[start:stop]] = predict(
                    model, matrices[name][pi[start:stop], pj[start:stop]])
            record['models'][name] = {key: value.tolist() if isinstance(value, np.ndarray) else value
                                      for key, value in model.items()}
        outputs['ridge_blend'][pi, pj] = sum(outputs[name][pi, pj] for name in
                                             ('ridge_price', 'ridge_interactions', 'ridge_fundamentals')) / 3
        folds.append(record)
        if callback:
            callback(record)
    return outputs, folds


def block_bootstrap_cagr(nav, dates, block=20, draws=1000, seed=261016):
    """Exploratory uncertainty; not a correction for repeated strategy search."""
    path = np.asarray(nav, float)
    daily = np.diff(np.log(path))
    if len(dates)!=len(daily) or not len(dates):
        raise ValueError('dates must match the daily return series')
    if len(daily) < 10 * block:
        return {'status': 'insufficient_independent_blocks'}
    rng = np.random.default_rng(seed)
    count = int(np.ceil(len(daily) / block))
    starts = rng.integers(0, len(daily) - block + 1, (draws, count))
    indices = (starts[:, :, None] + np.arange(block)).reshape(draws, -1)[:, :len(daily)]
    calendar_days=int((np.datetime64(str(dates[-1]))-np.datetime64(str(dates[0])))/np.timedelta64(1,'D'))+1
    sessions_per_calendar_year=len(daily)*365.25/calendar_days
    annualized = np.expm1(daily[indices].mean(axis=1) * sessions_per_calendar_year)
    return {'status': 'exploratory', 'block_sessions': block, 'draws': draws,
            'cagr_95pct_low': float(np.quantile(annualized, .025)),
            'cagr_95pct_high': float(np.quantile(annualized, .975)),
            'probability_cagr_above_20pct': float(np.mean(annualized > .2)),
            'selection_adjusted': False, 'annualization': 'ACT/365.25',
            'sessions_per_calendar_year': sessions_per_calendar_year}
