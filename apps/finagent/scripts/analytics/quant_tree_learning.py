"""Causal OHLCV features and purged tree/ridge ranking experiments.

Inspired by Qlib's multi-window price/volume families, with our own reduced
feature definitions and actual calendar. This is not an Alpha158 replication.
Missing earnings forecasts remain missing observations with explicit masks.
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

from quant_learning import fit_ridge, predict, labels, observation_weights
from strategy_research import lag, rolling, rank

MODEL_NAMES = ('ridge_extended', 'tree_return', 'tree_rank', 'tree_blend')
TREE_PARAMS = {
    'objective': 'regression', 'metric': 'l2', 'learning_rate': .04,
    'num_leaves': 31, 'max_depth': 6, 'min_data_in_leaf': 1000,
    'lambda_l1': 1., 'lambda_l2': 10., 'feature_fraction': .8,
    'bagging_fraction': .8, 'bagging_freq': 1, 'max_bin': 63,
    'num_threads': 4, 'seed': 261016, 'deterministic': True,
    'force_col_wise': True, 'verbosity': -1,
}


def divide(a, b):
    with np.errstate(divide='ignore', invalid='ignore'):
        result = np.asarray(a, float) / b
    return np.where(np.isfinite(result), result, np.nan)


def extended_features(data, features, forecasts=None):
    """Only information available by the signal day's close; no forward fills."""
    valid = (features['valid'] & (data['adjustment_quality'] > 0)
             & (data['adjustment_quality'] < 3))
    factor = np.where(valid, data['total_return_factor'], np.nan)
    opened, high, low, close = [data[k] * factor for k in ('open', 'high', 'low', 'close')]
    volume = np.where(valid & (data['volume'] > 0), data['volume'], np.nan)
    ret = features['returns'].copy()
    ret[~np.isfinite(ret) | (np.abs(ret) > .35)] = np.nan
    width = high - low
    cols, names = [], []

    def add(name, value):
        names.append(name)
        cols.append(np.where(np.isfinite(value), value, np.nan).astype(np.float32))

    add('body', divide(close - opened, opened))
    add('range', divide(width, opened))
    add('body_in_range', divide(close - opened, width))
    add('upper_shadow', divide(high - np.maximum(opened, close), opened))
    add('lower_shadow', divide(np.minimum(opened, close) - low, opened))
    add('close_in_range', divide(close - low, width))
    add('overnight', divide(opened, lag(close, 1)) - 1)
    add('log_price', np.log(np.maximum(data['close'], .01)))
    add('log_adv', np.log(np.maximum(features['adv'], 1.)))
    dvolume = np.log(np.maximum(divide(volume, lag(volume, 1)), .01))
    for window in (5, 10, 20, 60):
        minimum = max(3, int(np.ceil(.8 * window)))
        mean = rolling(close, window, 'mean', minimum)
        maximum = rolling(high, window, 'max', minimum)
        minimum_price = rolling(low, window, 'min', minimum)
        vmean = rolling(volume, window, 'mean', minimum)
        rmean = rolling(ret, window, 'mean', minimum)
        rstd = rolling(ret, window, 'std', minimum)
        abs_return = rolling(np.abs(ret), window, 'sum', minimum)
        up = rolling(np.maximum(ret, 0), window, 'sum', minimum)
        add(f'return_{window}', divide(close, lag(close, window)) - 1)
        add(f'mean_deviation_{window}', divide(close, mean) - 1)
        add(f'price_dispersion_{window}', divide(rolling(close, window, 'std', minimum), mean))
        add(f'from_high_{window}', divide(close, maximum) - 1)
        add(f'from_low_{window}', divide(close, minimum_price) - 1)
        add(f'range_position_{window}', divide(close - minimum_price, maximum - minimum_price))
        add(f'volatility_{window}', rstd)
        add(f'positive_fraction_{window}', rolling(np.where(np.isfinite(ret), (ret > 0).astype(float), np.nan), window, 'mean', minimum))
        add(f'gain_fraction_{window}', divide(up, abs_return))
        add(f'volume_ratio_{window}', divide(volume, vmean))
        add(f'volume_dispersion_{window}', divide(rolling(volume, window, 'std', minimum), vmean))
        add(f'illiquidity_{window}', rolling(divide(np.abs(ret), features['adv']), window, 'mean', minimum) * 1e8)
        covariance = rolling(ret * dvolume, window, 'mean', minimum) - rmean * rolling(dvolume, window, 'mean', minimum)
        correlation = divide(covariance, rstd * rolling(dvolume, window, 'std', minimum))
        add(f'return_volume_correlation_{window}', np.clip(correlation, -1, 1))
    add('momentum_120_skip5', divide(lag(close, 5), lag(close, 120)) - 1)
    add('momentum_252_skip20', divide(lag(close, 20), lag(close, 252)) - 1)
    add('volatility_ratio_20_60', divide(features['vol20'], rolling(ret, 60, 'std', 45)))
    prior = np.vstack([np.zeros((1, close.shape[1]), bool), features['eligible'][:-1]])
    market_mask = prior & np.isfinite(ret)
    count = market_mask.sum(axis=1)
    market_return = np.divide(np.where(market_mask, ret, 0).sum(axis=1), count,
                              out=np.zeros(len(count)), where=count > 0)
    market_index = np.cumprod(1 + market_return)
    for window in (20, 60):
        add(f'market_return_{window}', np.broadcast_to((divide(market_index, lag(market_index, window)) - 1)[:, None], close.shape))
    add('market_volatility', np.broadcast_to(features['market_vol'][:, None], close.shape))
    add('market_trend', np.broadcast_to(features['market_on'][:, None].astype(float), close.shape))
    if forecasts is not None:
        for key in ('value', 'revision'):
            available = np.isfinite(forecasts[key]) & features['eligible']
            add(f'{key}_rank', np.where(available, rank(forecasts[key], available), np.nan))
            add(f'{key}_available', available.astype(float))
    return np.stack(cols, axis=-1), names


def daily_rank_targets(target, discovery):
    """Reserve labels must not change any fitted target, including its rank."""
    return rank(target, np.isfinite(target) & discovery[None, :]) - .5


def purged_splits(dates, train_mask, test_year, horizon=20, validation_sessions=63, primary_since=None):
    """Both validation and test are separated from training label expiries."""
    from quant_learning import purged_training_dates
    permitted, first_test = purged_training_dates(dates, test_year, horizon, primary_since)
    positions = np.arange(len(dates))[:, None]
    train = train_mask & permitted[:, None]
    available_days = np.flatnonzero(train.any(axis=1))
    if len(available_days) < 126 + validation_sessions + horizon:
        raise ValueError('insufficient training and purged validation dates')
    validation_start = available_days[-validation_sessions]
    inner = train & (positions + horizon < validation_start)
    validation = train & (positions >= validation_start)
    if np.count_nonzero(inner.any(axis=1)) < 126:
        raise ValueError('inner training must contain 126 actual sessions')
    return train, inner, validation, first_test


def day_weights(indices):
    _, inverse, counts = np.unique(indices, return_inverse=True, return_counts=True)
    weights = 1. / counts[inverse]
    return weights / weights.mean()


def fit_tree(matrix, target, train, inner, validation, names, model_path=None, half_life_sessions=None):
    # Optional research dependency; importing the Agent never starts LightGBM.
    import lightgbm as lgb
    ii, ij = np.where(inner)
    vi, vj = np.where(validation)
    ti, tj = np.where(train)
    if len(ii) < 1000 or len(vi) < 1000 or not np.all(np.isfinite(target[ti, tj])):
        raise ValueError('insufficient or non-finite tree training labels')
    params = dict(TREE_PARAMS)
    dataset = lgb.Dataset(matrix[ii, ij], label=target[ii, ij], weight=observation_weights(ii, half_life_sessions), feature_name=names)
    validation_data = lgb.Dataset(matrix[vi, vj], label=target[vi, vj], weight=day_weights(vi), reference=dataset)
    selected = lgb.train(params, dataset, num_boost_round=400, valid_sets=[validation_data],
                         callbacks=[lgb.early_stopping(40, verbose=False)])
    iterations = selected.best_iteration
    complete = lgb.Dataset(matrix[ti, tj], label=target[ti, tj], weight=observation_weights(ti, half_life_sessions), feature_name=names)
    model = lgb.train(params, complete, num_boost_round=iterations)
    if model_path is not None:
        model.save_model(str(model_path))
    return model, {'iterations': iterations, 'validation_loss': selected.best_score['valid_0']['l2'],
                   'importance_gain': dict(zip(names, model.feature_importance('gain').tolist()))}


def walk_forward_trees(data, features, matrix, names, years, model_dir, horizon=20, callback=None):
    dates = data['dates']
    discovery = data['cohort'] == 'discovery'
    _, relative = labels(data, features, horizon)
    targets = {'tree_return': np.clip(relative, -.5, .5), 'tree_rank': daily_rank_targets(relative, discovery)}
    outputs = {key: np.full_like(data['close'], np.nan) for key in MODEL_NAMES}
    positions = np.arange(len(dates))[:, None]
    model_dir = Path(model_dir)
    model_dir.mkdir(exist_ok=True, parents=True)
    folds = []
    for year in years:
        mask = features['eligible'] & discovery[None, :] & np.isfinite(relative)
        train, inner, validation, first = purged_splits(dates, mask, year, horizon,
                                                       primary_since=data.get('primary_since'))
        test = features['eligible'] & (positions >= first - 1) & (dates[:, None] < f'{year + 1}-01-01')
        ti, tj = np.where(train)
        pi, pj = np.where(test)
        inner_days = np.flatnonzero(inner.any(axis=1))
        validation_days = np.flatnonzero(validation.any(axis=1))
        record = {'year': year, 'training_rows': len(ti), 'training_dates': len(np.unique(ti)),
                  'train_signal_first': str(dates[ti.min()]), 'train_label_last': str(dates[ti.max() + horizon]),
                  'test_first_open': str(dates[first]), 'validation_signal_first': str(dates[validation_days[0]]),
                  'validation_label_last': str(dates[validation_days[-1] + horizon]),
                  'inner_train_label_last': str(dates[inner_days[-1] + horizon]),
                  'inner_training_dates': len(inner_days), 'validation_dates': len(validation_days),
                  'test_predictions': len(pi), 'models': {}}
        ridge = fit_ridge(matrix[ti, tj], relative[ti, tj], ti)
        for begin in range(0, len(pi), 100000):
            ii, jj = pi[begin:begin + 100000], pj[begin:begin + 100000]
            outputs['ridge_extended'][ii, jj] = predict(ridge, matrix[ii, jj])
        record['models']['ridge_extended'] = {k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in ridge.items()}
        for name, target in targets.items():
            model, receipt = fit_tree(matrix, target, train, inner, validation, names, model_dir / f'{year}-{name}.txt')
            for begin in range(0, len(pi), 100000):
                ii, jj = pi[begin:begin + 100000], pj[begin:begin + 100000]
                outputs[name][ii, jj] = model.predict(matrix[ii, jj], num_threads=TREE_PARAMS['num_threads'])
            record['models'][name] = receipt
            if callback:
                callback({'phase': 'model', 'year': year, 'model': name, 'iterations': receipt['iterations']})
        # Fixed blend of model ranks prevents return and percentile scales from
        # silently determining the ensemble weight. Only contemporaneous scores.
        blended = sum(rank(outputs[k], features['eligible']) for k in MODEL_NAMES[:3]) / 3
        outputs['tree_blend'][pi, pj] = blended[pi, pj]
        folds.append(record)
        if callback:
            callback({'phase': 'fold', **record})
    return outputs, folds
