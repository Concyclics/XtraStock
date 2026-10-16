"""Purged rolling refits with disjoint next-open prediction ownership.

This controlled cadence/recency study is not a meta-learning implementation.
Every completed fold is independently resumable under its frozen protocol.
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

from quant_learning import fit_ridge, predict, labels
from quant_tree_learning import daily_rank_targets, fit_tree, tree_parameters, TREE_PARAMS
from strategy_dataset import checksum, dump
from strategy_research import rank


def prediction_periods(dates, years, cadence):
    dates = np.asarray(dates)
    if cadence not in ('annual', 'quarterly', 'monthly'):
        raise ValueError('unsupported refit cadence')
    if len(dates) < 2 or not np.all(dates[1:] > dates[:-1]):
        raise ValueError('refits require a unique increasing calendar')
    months = {'annual': (1,), 'quarterly': (1, 4, 7, 10), 'monthly': range(1, 13)}[cadence]
    years = sorted(set(years))
    if not years or any(b != a + 1 for a, b in zip(years, years[1:])):
        raise ValueError('evaluation years must be nonempty and contiguous')
    starts = []
    for year in years:
        before = len(starts)
        for month in months:
            day = f'{year:04}-{month:02}-01'
            first = int(np.searchsorted(dates, day))
            if first < len(dates) and str(dates[first])[:4] == str(year):
                starts.append((day, first))
        if len(starts) == before:
            raise ValueError('requested evaluation year has no sessions')
    starts = sorted(set(starts))
    periods = []
    for i, (day, first) in enumerate(starts):
        if first == 0:
            raise ValueError('first evaluation open requires an earlier signal close')
        end = starts[i + 1][1] - 1 if i + 1 < len(starts) else int(np.searchsorted(dates, f'{years[-1]+1}-01-01'))
        if end <= first - 1:
            raise ValueError('empty or duplicate refit period')
        periods.append({'period_start': day, 'year': int(day[:4]), 'first_open_index': first,
                        'signal_begin': first - 1, 'signal_end_exclusive': end})
    if not periods:
        raise ValueError('no evaluation periods')
    return periods


def rolling_splits(dates, mask, period, primary_since, *, horizon=20, training_years=3, validation_sessions=63):
    first = period['first_open_index']
    if horizon < 1 or training_years < 1 or validation_sessions < 1:
        raise ValueError('invalid training horizon or history')
    lower = max(primary_since, (pd.Timestamp(period['period_start']) - pd.DateOffset(years=training_years)).strftime('%Y-%m-%d'))
    indices = np.arange(len(dates))[:, None]
    train = mask & (dates[:, None] >= lower) & (indices + horizon < first)
    days = np.flatnonzero(train.any(axis=1))
    if len(days) < 126 + validation_sessions + horizon:
        raise ValueError('insufficient purged rolling training history')
    validation_start = days[-validation_sessions]
    inner = train & (indices + horizon < validation_start)
    validation = train & (indices >= validation_start)
    if np.count_nonzero(inner.any(axis=1)) < 126:
        raise ValueError('insufficient inner training sessions')
    return train, inner, validation


def _predict_block(model, matrix, eligible, begin, end, *, tree=False):
    block = np.full((end - begin, matrix.shape[1]), np.nan)
    pi, pj = np.where(eligible[begin:end])
    for start in range(0, len(pi), 100000):
        ii, jj = pi[start:start + 100000], pj[start:start + 100000]
        x = matrix[ii + begin, jj]
        block[ii, jj] = model.predict(x, num_threads=TREE_PARAMS['num_threads']) if tree else predict(model, x)
    return block


def walk_forward_adaptation(data, features, matrix, names, years, variants, output, protocol_hash,
                            *, horizon=20, training_years=3, validation_sessions=63, callback=None):
    import json
    output = Path(output)
    dates = data['dates']
    _, target = labels(data, features, horizon)
    discovery = data['cohort'] == 'discovery'
    mask = features['eligible'] & discovery[None, :] & np.isfinite(target)
    tree_targets = {'tree_return': np.clip(target, -.5, .5), 'tree_rank': daily_rank_targets(target, discovery)}
    outputs, folds = {}, []
    for variant, config in variants.items():
        family = config['family']
        if family not in ('ridge_extended', 'tree_blend'):
            raise ValueError('unsupported refit model family')
        half_life = config['half_life_sessions']
        if 'tree_seed' in config:
            tree_parameters(config['tree_seed'])
            if family != 'tree_blend':
                raise ValueError('tree seed requires a tree model family')
        score = np.full_like(data['close'], np.nan)
        directory = output / 'folds' / variant
        directory.mkdir(parents=True, exist_ok=True)
        periods = prediction_periods(dates, years, config['cadence'])
        for period in periods:
            begin, end = period['signal_begin'], period['signal_end_exclusive']
            path = directory / (period['period_start'] + '.npz')
            receipt_path = path.with_suffix('.json')
            if path.exists() or receipt_path.exists():
                if not (path.exists() and receipt_path.exists()):
                    raise ValueError('partial fold checkpoint requires explicit review')
                receipt = json.loads(receipt_path.read_text())
                if receipt['protocol_sha256'] != protocol_hash or receipt['sha256'] != checksum(path):
                    raise ValueError('fold checkpoint provenance differs')
                record = receipt['fold']
                if any(record[key] != value for key, value in period.items()):
                    raise ValueError('fold checkpoint period differs')
                if record.get('tree_seed') != config.get('tree_seed'):
                    raise ValueError('fold checkpoint tree seed differs')
                for model_file, digest in receipt['model_files'].items():
                    if checksum(directory / model_file) != digest:
                        raise ValueError('saved model differs from completed fold')
                with np.load(path, allow_pickle=False) as z:
                    block = z['score']
            else:
                train, inner, validation = rolling_splits(dates, mask, period, data['primary_since'], horizon=horizon,
                    training_years=training_years, validation_sessions=validation_sessions)
                ti, tj = np.where(train)
                ii = np.flatnonzero(inner.any(axis=1))
                vi = np.flatnonzero(validation.any(axis=1))
                record = {**period, 'variant': variant, 'family': family, 'cadence': config['cadence'],
                          'half_life_sessions': half_life, 'training_rows': len(ti),
                          'training_dates': len(np.unique(ti)), 'train_signal_first': str(dates[ti.min()]),
                          'train_signal_last': str(dates[ti.max()]), 'train_label_last': str(dates[ti.max() + horizon]),
                          'test_first_open': str(dates[period['first_open_index']]),
                          'test_first_signal': str(dates[begin]), 'test_last_signal': str(dates[end - 1]),
                          'test_predictions': int(features['eligible'][begin:end].sum()),
                          'validation_signal_first': str(dates[vi[0]]), 'validation_label_last': str(dates[vi[-1]+horizon]),
                          'inner_train_label_last': str(dates[ii[-1]+horizon]),
                          'inner_training_dates': len(ii), 'validation_dates': len(vi), 'models': {}}
                if 'tree_seed' in config:
                    record['tree_seed'] = config['tree_seed']
                ridge = fit_ridge(matrix[ti, tj], target[ti, tj], ti, half_life_sessions=half_life)
                block = _predict_block(ridge, matrix, features['eligible'], begin, end)
                record['models']['ridge_extended'] = {k: v.tolist() if isinstance(v, np.ndarray) else v for k, v in ridge.items()}
                model_files = {}
                if family == 'tree_blend':
                    component_blocks = [block]
                    for key, tree_target in tree_targets.items():
                        model_path = directory / f"{period['period_start']}-{key}.txt"
                        seed_args = {'tree_seed': config['tree_seed']} if 'tree_seed' in config else {}
                        model, fitted = fit_tree(matrix, tree_target, train, inner, validation, names,
                                                  model_path, half_life_sessions=half_life, **seed_args)
                        component_blocks.append(_predict_block(model, matrix, features['eligible'], begin, end, tree=True))
                        record['models'][key] = fitted
                        model_files[model_path.name] = checksum(model_path)
                        if callback:
                            callback({'phase': 'model', 'variant': variant, 'period': period['period_start'],
                                      'model': key, 'iterations': fitted['iterations']})
                    block = sum(rank(b, features['eligible'][begin:end]) for b in component_blocks) / 3
                temp = path.with_suffix('.tmp.npz')
                np.savez_compressed(temp, score=block)
                temp.replace(path)
                dump(receipt_path, {'protocol_sha256': protocol_hash, 'sha256': checksum(path),
                                    'fold': record, 'model_files': model_files})
            if block.shape != (end - begin, matrix.shape[1]):
                raise ValueError('fold prediction shape differs from its calendar')
            if not np.array_equal(np.isfinite(block), features['eligible'][begin:end]):
                raise ValueError('fold prediction coverage differs from eligible securities')
            score[begin:end] = block
            folds.append(record)
            dump(output / 'TRAINING_FOLDS.json', folds)
            if callback:
                callback({'phase': 'fold', 'variant': variant, 'period': period['period_start'],
                          'training_rows': record['training_rows'], 'training_dates': record['training_dates'],
                          'train_label_last': record['train_label_last'], 'completed_folds': len(folds)})
        outputs[variant] = score
    return outputs, folds
