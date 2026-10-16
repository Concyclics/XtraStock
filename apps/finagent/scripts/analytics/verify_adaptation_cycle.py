"""Verify rolling prediction ownership, maturity, provenance and diagnostics.

Portfolio cash accounting is checked separately by verify_quant_cycle.py.
Rank correlations here are overlapping-label diagnostics, not trading returns.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import numpy as np
import pandas as pd

from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features, rank
from quant_learning import labels


def audit_predictions(directory, data, features, reference):
    directory, reference = Path(directory), Path(reference)
    spec = json.loads((directory / 'PROTOCOL.json').read_text())
    completed = json.loads((directory / 'COMPLETE.json').read_text())
    digest = checksum(directory / 'PROTOCOL.json')
    if digest != completed['protocol_sha256']:
        raise ValueError('prediction protocol differs from completion')
    for name, expected in spec['code'].items():
        if checksum(directory / 'code-at-execution' / name) != expected:
            raise ValueError('prediction code differs from its frozen receipt')
    if checksum(directory / 'STUDY_CONFIG.json') != spec['study_config_sha256']:
        raise ValueError('study configuration changed')
    study = json.loads((directory / 'STUDY_CONFIG.json').read_text())
    if (completed['status'] != 'adaptation_predictions_complete'
            or spec['training_variants'] != study['variants']
            or any(spec[key] != study[key] for key in ('models', 'training_years', 'validation_sessions'))
            or data['primary_since'] != spec['primary_since']):
        raise ValueError('training did not preserve the declared study')
    if spec['horizon'] != study.get('label_horizon', spec['horizon']):
        raise ValueError('Training label horizon differs from the study')
    for key in ('prediction_reference_map', 'score_blends', 'model_horizons', 'diagnostic_horizons', 'diagnostic_next_open'):
        if spec.get(key) != study.get(key):
            raise ValueError('Horizon construction differs from the study: ' + key)
    if spec.get('model_horizons'):
        from horizon_signals import validate_study
        validate_study(study)
    receipt = json.loads((directory / 'PREDICTION_RECEIPT.json').read_text())
    if receipt['protocol_sha256'] != digest or receipt['sha256'] != checksum(directory / 'PREDICTIONS.npz'):
        raise ValueError('prediction payload receipt differs')
    if checksum(reference / 'PREDICTIONS.npz') != spec['annual_reference_predictions_sha256']:
        raise ValueError('annual prediction reference changed')
    if checksum(reference / 'PROTOCOL.json') != spec['annual_reference_protocol_sha256']:
        raise ValueError('annual training reference changed')
    with np.load(directory / 'PREDICTIONS.npz', allow_pickle=False) as z:
        scores = {name: z[name] for name in z.files}
    if set(scores) != set(spec['models']) or any(s.shape != data['close'].shape for s in scores.values()):
        raise ValueError('declared prediction families or dimensions differ')
    with np.load(reference / 'PREDICTIONS.npz', allow_pickle=False) as z:
        reference_map = spec.get('prediction_reference_map', {'annual_tree_blend': 'tree_blend', 'annual_ridge_extended': 'ridge_extended'})
        for current, old in reference_map.items():
            if not np.array_equal(scores[current], z[old], equal_nan=True):
                raise ValueError('annual control scores were changed')
    if spec.get('score_blends'):
        from horizon_signals import blend_scores
        components = {name: value for name, value in scores.items() if name not in spec['score_blends']}
        for name, expected in blend_scores(components, features['eligible'], spec['score_blends']).items():
            if not np.array_equal(scores[name], expected, equal_nan=True):
                raise ValueError('Horizon blend differs from fixed contemporaneous components')
    dates = data['dates']
    outcome, relative = labels(data, features, spec['horizon'])
    mask = features['eligible'] & (data['cohort'][None, :] == 'discovery') & np.isfinite(relative)
    per_day = mask.sum(axis=1)
    folds = json.loads((directory / 'TRAINING_FOLDS.json').read_text())
    first = int(np.searchsorted(dates, f"{min(spec['test_years'])}-01-01"))
    limit = int(np.searchsorted(dates, f"{max(spec['test_years'])+1}-01-01"))
    horizon = spec['horizon']
    audited = []
    for name, cfg in spec['training_variants'].items():
        owned = np.zeros(len(dates), int)
        records = [fold for fold in folds if fold['variant'] == name]
        period_code = {'annual': 'Y', 'quarterly': 'Q', 'monthly': 'M'}[cfg['cadence']]
        observed_periods = pd.PeriodIndex(pd.to_datetime(dates[first:limit]), freq=period_code).unique()
        expected_starts = {p.start_time.strftime('%Y-%m-%d') for p in observed_periods}
        if {r['period_start'] for r in records} != expected_starts or len(records) != len(expected_starts):
            raise ValueError('missing or duplicate training period')
        for record in records:
            start = int(np.searchsorted(dates, record['period_start']))
            lower = max(spec['primary_since'], (pd.Timestamp(record['period_start']) - pd.DateOffset(years=spec['training_years'])).strftime('%Y-%m-%d'))
            days = np.flatnonzero((dates >= lower) & (np.arange(len(dates)) + horizon < start) & (per_day > 0))
            valid_days = days[-spec['validation_sessions']:]
            inner_days = days[days + horizon < valid_days[0]]
            expected = {'year': int(record['period_start'][:4]), 'cadence': cfg['cadence'], 'family': cfg['family'],
                'half_life_sessions': cfg['half_life_sessions'], 'first_open_index': start,
                'signal_begin': start - 1, 'train_signal_first': str(dates[days[0]]),
                'train_signal_last': str(dates[days[-1]]), 'train_label_last': str(dates[days[-1]+horizon]),
                'training_rows': int(per_day[days].sum()), 'training_dates': len(days),
                'test_first_open': str(dates[start]), 'test_first_signal': str(dates[start-1]),
                'validation_signal_first': str(dates[valid_days[0]]), 'validation_label_last': str(dates[valid_days[-1]+horizon]),
                'inner_train_label_last': str(dates[inner_days[-1]+horizon]),
                'inner_training_dates': len(inner_days), 'validation_dates': len(valid_days)}
            if any(record[k] != v for k, v in expected.items()):
                raise ValueError('actual eligible training history differs from fold receipt: ' + name + record['period_start'])
            begin, end = record['signal_begin'], record['signal_end_exclusive']
            following = (pd.Period(record['period_start'], freq=period_code) + 1).start_time.strftime('%Y-%m-%d')
            following_index = int(np.searchsorted(dates, following))
            expected_end = limit if following_index >= limit else following_index - 1
            if (not begin < end <= limit or end != expected_end
                    or record['test_last_signal'] != str(dates[end-1])
                    or record['test_predictions'] != int(features['eligible'][begin:end].sum())):
                raise ValueError('invalid prediction ownership interval')
            owned[begin:end] += 1
            path = directory / 'folds' / name / (record['period_start'] + '.npz')
            saved = json.loads(path.with_suffix('.json').read_text())
            if saved['protocol_sha256'] != digest or saved['sha256'] != checksum(path) or saved['fold'] != record:
                raise ValueError('fold checkpoint differs from training record')
            for file, model_digest in saved['model_files'].items():
                if checksum(path.parent / file) != model_digest:
                    raise ValueError('saved fitted model changed')
            with np.load(path, allow_pickle=False) as z:
                if not np.array_equal(z['score'], scores[name][begin:end], equal_nan=True):
                    raise ValueError('final score differs from per-fold checkpoint')
            audited.append({'variant': name, 'period': record['period_start'], 'training_rows': record['training_rows'],
                            'maturity_and_ownership_match': True})
        if not ((owned[first-1:limit] == 1).all() and (owned[:first-1] == 0).all() and (owned[limit:] == 0).all()):
            raise ValueError('prediction ownership has gaps, overlaps or extra dates')
        expected_finite = features['eligible'].copy()
        expected_finite[:first-1] = False
        expected_finite[limit:] = False
        if not np.array_equal(np.isfinite(scores[name]), expected_finite):
            raise ValueError('prediction eligibility coverage differs')
    if len(audited) != completed['completed_training_folds'] or len(audited) != len(folds):
        raise ValueError('completed training count differs')
    return scores, outcome, {'status': 'rolling_predictions_verified', 'verified_at': datetime.now(timezone.utc).isoformat(),
        'folds': len(audited), 'checks': audited, 'annual_controls_unchanged': True,
        'future_labels_excluded_from_training': True, 'source_quality_passed': False, 'production_approved': False}


def predictive_diagnostics(data, features, scores, outcome, years, horizon=20, *, next_open=False):
    rows = []
    dates = data['dates']
    report_dates = np.concatenate((dates[1:], np.array(['']))) if next_open else dates
    for model, score in scores.items():
        for cohort in ('all', 'discovery', 'cross_section_reserve'):
            member = np.ones(len(data['symbols']), bool) if cohort == 'all' else data['cohort'] == cohort
            usable = features['eligible'] & member[None, :] & np.isfinite(score) & np.isfinite(outcome)
            a, b = rank(score, usable), rank(outcome, usable)
            n = usable.sum(axis=1)
            # Center both rank vectors within the actually labeled sample.
            ma = np.divide(np.nansum(a, axis=1), n, out=np.full(len(n), np.nan), where=n>0)
            mb = np.divide(np.nansum(b, axis=1), n, out=np.full(len(n), np.nan), where=n>0)
            da, db = a - ma[:, None], b - mb[:, None]
            denom = np.sqrt(np.nansum(da*da, axis=1)*np.nansum(db*db, axis=1))
            correlation = np.divide(np.nansum(da*db, axis=1), denom, out=np.full(len(n), np.nan), where=denom>0)
            correlation[n<30] = np.nan
            for year in [*years, 'all']:
                selected = (report_dates >= f'{min(years)}-01-01') & (report_dates < f'{max(years)+1}-01-01')
                if year != 'all':
                    selected &= np.char.startswith(report_dates.astype(str), str(year))
                values = correlation[selected & np.isfinite(correlation)]
                rows.append({'model': model, 'cohort': cohort, 'year': year, 'horizon': horizon,
                    'eligible_predictions': int((features['eligible'][selected] & member[None, :] & np.isfinite(score[selected])).sum()),
                    'labeled_predictions': int(usable[selected].sum()), 'labeled_days': len(values),
                    'mean_daily_rank_ic': float(values.mean()) if len(values) else None,
                    'positive_daily_rank_ic_fraction': float((values>0).mean()) if len(values) else None,
                    **({'date_ownership': 'next_open', 'allocation': 'prediction_diagnostic'} if next_open else {}),
                    'label_contract': f'Overlapping {horizon}-session next-open returns; diagnostic only, not executed profit.'})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiment', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--reference-experiment', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads((args.experiment / 'PROTOCOL.json').read_text())
    for name, digest in spec['source'].items():
        if checksum(args.dataset / name) != digest:
            raise ValueError('verification market differs from frozen training input')
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    scores, outcome, report = audit_predictions(args.experiment, data, features, args.reference_experiment)
    horizons = spec.get('diagnostic_horizons', [spec['horizon']])
    rows = []
    for horizon in horizons:
        actual = outcome if horizon == spec['horizon'] else labels(data, features, horizon)[0]
        rows.extend(predictive_diagnostics(data, features, scores, actual, spec['test_years'], horizon,
                                           next_open=bool(spec.get('diagnostic_next_open'))))
    pd.DataFrame(rows).to_csv(args.experiment / 'PREDICTIVE_METRICS.csv', index=False)
    dump(args.report, report)
    print(json.dumps({k: v for k, v in report.items() if k != 'checks'}))


if __name__ == '__main__':
    main()
