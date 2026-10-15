"""CPU-only, date-purged forecasting experiments and paired validation.

All hyperparameters are selected on tuning dates. Outer tests are retrospective
research, not fresh holdouts or proof of executable investment returns.
"""
from __future__ import annotations

import math
import numpy as np

VERSION = 'purged-robust-volatility-v3'
CANDIDATES = tuple({'family': family, 'alpha': alpha, 'window': window}
                   for family in ('ridge_raw', 'huber_risk_normalized')
                   for alpha in (100., 1000.) for window in (252, None))
BASELINE_NAMES = ('zero', 'training_median', 'momentum', 'reversal')


def date_means(rows, values):
    groups = {}
    for row, value in zip(rows, values):
        groups.setdefault(row['signal_date'], []).append(float(value))
    return np.array([np.mean(groups[d]) for d in sorted(groups)])


def risk_scale(rows, horizon):
    return np.clip(np.asarray([r['x'][2] for r in rows], float), .005, .15) * math.sqrt(horizon)


def feature_matrix(rows, engineered=False):
    x = np.asarray([r['x'] for r in rows], float)
    if not np.isfinite(x).all() or x.ndim != 2 or x.shape[1] != 5:
        raise ValueError('finite five-feature price rows required')
    if not engineered:
        return x
    vol = np.clip(x[:, 2], .005, .15)
    log_volume = np.log(np.clip(x[:, 4], .05, 20))
    return np.column_stack([x, x[:, 0] / (vol * math.sqrt(5)),
        x[:, 1] / (vol * math.sqrt(20)), (x[:, 0] - x[:, 1] / 4) / vol,
        x[:, 3] / vol, log_volume, x[:, 0] * log_volume / vol])


def fit_candidate(rows, spec, horizon):
    dates = sorted({r['signal_date'] for r in rows})
    if spec['window']:
        first = dates[-min(len(dates), spec['window'])]
        rows = [r for r in rows if r['signal_date'] >= first]
    robust = spec['family'] == 'huber_risk_normalized'
    x = feature_matrix(rows, robust)
    y = np.asarray([r['y'] for r in rows], float)
    scale = risk_scale(rows, horizon) if robust else np.ones(len(rows))
    if not np.isfinite(y).all():
        raise ValueError('non-finite outcomes')
    counts = {}
    for r in rows:
        counts[r['signal_date']] = counts.get(r['signal_date'], 0) + 1
    weights = np.array([1 / counts[r['signal_date']] for r in rows])
    weights /= weights.mean()
    mean = np.average(x, axis=0, weights=weights)
    std = np.sqrt(np.average((x - mean) ** 2, axis=0, weights=weights))
    std = np.where(std > 1e-8, std, 1.)
    z = np.column_stack([np.ones(len(rows)), np.clip((x - mean) / std, -8, 8)])
    target = y / scale
    penalty = np.eye(z.shape[1]) * spec['alpha']; penalty[0, 0] = 0
    beta = np.zeros(z.shape[1]); beta[0] = np.median(target)
    # Huber IRLS reduces the influence of extreme returns. No test labels or
    # calibration residuals affect its fixed delta or feature transformation.
    for _ in range(30 if robust else 1):
        residual = target - z @ beta
        w = weights * (np.minimum(1., .75 / np.maximum(np.abs(residual), 1e-12)) if robust else 1.)
        new = np.linalg.solve(z.T @ (w[:, None] * z) + penalty, z.T @ (w * target))
        if np.max(np.abs(new - beta)) < 1e-8:
            beta = new; break
        beta = new
    return {'kind': VERSION, 'spec': dict(spec), 'horizon': horizon, 'mean': mean.tolist(),
        'std': std.tolist(), 'beta': beta.tolist(), 'alpha': spec['alpha'],
        'trained_signal_through': max(r['signal_date'] for r in rows),
        'trained_outcome_through': max(r['exit_date'] for r in rows),
        'training_dates': len(counts)}


def predict_candidate(model, rows):
    robust = model['spec']['family'] == 'huber_risk_normalized'
    x = feature_matrix(rows, robust)
    z = np.column_stack([np.ones(len(rows)), np.clip((x - model['mean']) / model['std'], -8, 8)])
    result = z @ model['beta']
    return result * risk_scale(rows, model['horizon']) if robust else result


def calibrate(model, rows):
    residual = (np.asarray([r['y'] for r in rows]) - predict_candidate(model, rows)) / risk_scale(rows, model['horizon'])
    # Signed empirical quantiles, not a guarantee under exchangeability. Coverage
    # and interval score are measured on separate dates before any display gate.
    model['normalized_residual_interval_80'] = [float(np.quantile(residual, .1, method='lower')),
                                               float(np.quantile(residual, .9, method='higher'))]
    model['interval_method'] = 'volatility_scaled_empirical_residual_quantiles'
    model['calibration_signal_from'] = min(r['signal_date'] for r in rows)
    model['calibration_outcome_through'] = max(r['exit_date'] for r in rows)
    return model


def intervals(model, rows, prediction=None):
    pred = predict_candidate(model, rows) if prediction is None else np.asarray(prediction)
    scale = risk_scale(rows, model['horizon'])
    lo, hi = model['normalized_residual_interval_80']
    return pred + lo * scale, pred + hi * scale


def baseline_predictions(rows, horizon, training_median):
    momentum = np.array([r['x'][1] * horizon / 20 for r in rows])
    return {'zero': np.zeros(len(rows)), 'training_median': np.full(len(rows), training_median),
            'momentum': momentum, 'reversal': -momentum}


def block_interval(daily_values, horizon, draws=2000):
    values = np.asarray(daily_values, float)
    if not len(values) or not np.isfinite(values).all():
        raise ValueError('finite nonempty date-level differences required')
    width = min(horizon, len(values)); blocks = math.ceil(len(values) / width)
    rng = np.random.default_rng(461903)
    starts = rng.integers(0, len(values), size=(draws, blocks))
    indexes = (starts[:, :, None] + np.arange(width)) % len(values)
    samples = values[indexes.reshape(draws, -1)[:, :len(values)]].mean(axis=1)
    return {'mean': float(values.mean()), 'lower_95': float(np.quantile(samples, .025)),
            'upper_95': float(np.quantile(samples, .975)), 'block_sessions': width,
            'date_groups': len(values), 'effective_blocks_approx': len(values) // width,
            'draws': draws, 'method': 'paired_date_level_circular_block_bootstrap'}


def ranked_backtest(rows, scores, cost_bps=20):
    """Fully liquidated, nonoverlapping next-open/exit-close basket diagnostic.

    Same-cohort equal weighting is the investable comparator. Missing bars were
    rejected upstream; fills/limits/suspensions are NOT simulated here.
    """
    groups = {}
    for row, score in zip(rows, scores):
        groups.setdefault(row['signal_date'], []).append((row, float(score)))
    holdings = []; previous_exit = None
    for day in sorted(groups):
        if previous_exit is not None and day < previous_exit:
            continue
        group = groups[day]
        if len(group) < 5:
            continue
        # A common holding window is required for a meaningful basket return.
        if len({(r['entry_date'], r['exit_date']) for r, _ in group}) != 1:
            raise ValueError('inconsistent basket holding dates')
        ordered = sorted(group, key=lambda p: (-p[1], p[0]['stock_code']))
        selected = ordered[:max(1, math.ceil(len(group) * .2))]
        gross = float(np.mean([r['y'] for r, _ in selected]))
        benchmark = float(np.mean([r['y'] for r, _ in group]))
        cost = cost_bps / 10000
        holdings.append({'signal_date': day, 'entry_date': selected[0][0]['entry_date'],
            'exit_date': selected[0][0]['exit_date'], 'stock_codes': [r['stock_code'] for r, _ in selected],
            'gross_return': gross, 'net_return': gross - cost, 'equal_weight_net_return': benchmark - cost,
            'excess_return': gross - benchmark})
        previous_exit = holdings[-1]['exit_date']
    def curve(key):
        nav = np.r_[1., np.cumprod([1 + r[key] for r in holdings])]
        return {'cumulative_return': float(nav[-1] - 1),
                'max_drawdown': float(np.min(nav / np.maximum.accumulate(nav) - 1))}
    return {'holding_windows': len(holdings), 'cost_bps_round_trip': cost_bps,
        'strategy': curve('net_return'), 'equal_weight': curve('equal_weight_net_return'),
        'mean_window_excess': float(np.mean([r['excess_return'] for r in holdings])) if holdings else None,
        'windows': holdings, 'executable_result': False,
        'limitations': '当前样本回溯；每期全额换仓的固定费用情景，未模拟涨跌停、冲击成本或真实成交'}


def split_rows(rows, offset=0, horizon=5, test_days=126):
    dates = sorted({r['signal_date'] for r in rows})
    # Longer horizons need more raw tuning/calibration dates to retain enough
    # matured labels after purging, without lowering the minimum date count.
    tune_days = max(63, horizon + 25)
    calibration_days = max(63, 6 * horizon)
    end = len(dates) - offset
    if end < 252 + tune_days + calibration_days + test_days + horizon:
        raise ValueError('insufficient history for purged outer fold')
    test_first, cal_first, tune_first = dates[end-test_days], dates[end-test_days-calibration_days], dates[end-test_days-calibration_days-tune_days]
    pieces = {
        'train': [r for r in rows if r['signal_date'] < tune_first and r['exit_date'] < tune_first],
        'tune': [r for r in rows if tune_first <= r['signal_date'] < cal_first and r['exit_date'] < cal_first],
        'calibration': [r for r in rows if cal_first <= r['signal_date'] < test_first and r['exit_date'] < test_first],
        'test': [r for r in rows if test_first <= r['signal_date'] <= dates[end-1]],
    }
    for name, minimum in (('train', 252), ('tune', 25), ('calibration', 25), ('test', test_days)):
        if len({r['signal_date'] for r in pieces[name]}) < minimum:
            raise ValueError('insufficient purged dates: ' + name)
    return pieces


def evaluate_fold(pieces, horizon):
    trials = []
    for spec in CANDIDATES:
        model = fit_candidate(pieces['train'], spec, horizon)
        losses = np.abs(np.array([r['y'] for r in pieces['tune']]) - predict_candidate(model, pieces['tune']))
        trials.append({'spec': dict(spec), 'tune_daily_mae': float(date_means(pieces['tune'], losses).mean()), 'model': model})
    winner = min(trials, key=lambda t: t['tune_daily_mae'])
    model = calibrate(winner['model'], pieces['calibration'])
    median = float(np.median([r['y'] for r in pieces['train']]))
    tune_baselines = baseline_predictions(pieces['tune'], horizon, median)
    tune_y = np.array([r['y'] for r in pieces['tune']])
    baseline = min(BASELINE_NAMES, key=lambda n: date_means(pieces['tune'], abs(tune_y-tune_baselines[n])).mean())
    rows = pieces['test']; actual = np.array([r['y'] for r in rows])
    pred = predict_candidate(model, rows); lo, hi = intervals(model, rows, pred)
    baselines = baseline_predictions(rows, horizon, median)
    error = abs(actual-pred); covered = (actual >= lo) & (actual <= hi)
    baseline_maes = {name: float(abs(actual-values).mean()) for name, values in baselines.items()}
    ci = block_interval(date_means(rows, error-abs(actual-baselines[baseline])), horizon)
    direction_ci = block_interval(date_means(rows, (np.sign(actual) == np.sign(pred)).astype(float) -
        (np.sign(actual) == (1 if median >= 0 else -1)).astype(float)), horizon)
    # The interval score penalizes both missed observations and unnecessarily wide intervals.
    interval_score = hi-lo + 10*np.maximum(lo-actual, 0) + 10*np.maximum(actual-hi, 0)
    cal_rows = pieces['calibration']; cal_y = np.array([r['y'] for r in cal_rows])
    cal_base = baseline_predictions(cal_rows, horizon, median)[baseline]
    bq = np.quantile((cal_y-cal_base)/risk_scale(cal_rows, horizon), [.1, .9])
    blo, bhi = (baselines[baseline]+q*risk_scale(rows, horizon) for q in bq)
    bscore = bhi-blo + 10*np.maximum(blo-actual, 0) + 10*np.maximum(actual-bhi, 0)
    score = {'rows': len(rows), 'dates': len({r['signal_date'] for r in rows}),
        'start': min(r['signal_date'] for r in rows), 'end': max(r['signal_date'] for r in rows),
        'outcomes_through': max(r['exit_date'] for r in rows),
        'mae': float(error.mean()), 'date_weighted_mae': float(date_means(rows, error).mean()),
        'zero_return_mae': baseline_maes['zero'], 'momentum_mae': baseline_maes['momentum'],
        'baseline_maes': baseline_maes, 'selected_baseline': baseline,
        'rmse': float(np.sqrt(np.mean((actual-pred)**2))),
        'coverage_80': float(covered.mean()), 'date_weighted_coverage_80': float(date_means(rows, covered).mean()),
        'interval_mean_width': float(np.mean(hi-lo)), 'interval_score_80': float(interval_score.mean()),
        'baseline_interval_score_80': float(bscore.mean()),
        'direction_accuracy': float((np.sign(actual) == np.sign(pred)).mean()),
        'positive_return_rate': float((actual > 0).mean()),
        'paired_mae_difference': ci, 'direction_advantage': direction_ci,
        'mean_daily_selected_baseline_mae_difference': ci['mean'],
        'beats_all_baselines': float(error.mean()) < min(baseline_maes.values())}
    boundaries = {name: {'rows': len(part), 'dates': len({r['signal_date'] for r in part}),
        'signal_start': min(r['signal_date'] for r in part), 'signal_end': max(r['signal_date'] for r in part),
        'outcome_end': max(r['exit_date'] for r in part)} for name, part in pieces.items()}
    return {'model': model, 'final_test': score, 'splits': boundaries,
        'selected_spec': winner['spec'], 'training_median': median,
        'tuning_trials': [{k: v for k, v in t.items() if k != 'model'} for t in trials],
        'backtest': ranked_backtest(rows, pred), 'cost_stress': ranked_backtest(rows, pred, 50)}


def train_validated(rows, horizon):
    keys = [(r['stock_code'], r['signal_date']) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError('duplicate stock/date rows')
    folds = []
    for offset in (252, 126, 0):
        try:
            parts = split_rows(rows, offset, horizon)
        except ValueError:
            if offset == 0:
                raise
            continue
        folds.append(evaluate_fold(parts, horizon))
    latest = folds[-1]; test = latest['final_test']
    wins = sum(f['final_test']['beats_all_baselines'] for f in folds)
    checks = {
        'latest_beats_simple_baselines': test['beats_all_baselines'],
        'coverage_80_between_75_and_90': .75 <= test['coverage_80'] <= .90,
        'three_complete_outer_folds': len(folds) == 3,
        'at_least_two_fold_wins': wins >= 2,
        'paired_improvement_confident': test['paired_mae_difference']['upper_95'] < 0,
        'interval_score_not_worse': test['interval_score_80'] <= test['baseline_interval_score_80'],
        'five_calibration_holding_windows': latest['splits']['calibration']['dates'] >= 5*horizon,
    }
    passed = all(checks.values())
    dates = sorted({r['signal_date'] for r in rows}); cutoff = dates[-max(63, 6*horizon)]
    refresh_train = [r for r in rows if r['exit_date'] < cutoff]
    refresh_cal = [r for r in rows if r['signal_date'] >= cutoff]
    refreshed = calibrate(fit_candidate(refresh_train, latest['selected_spec'], horizon), refresh_cal)
    evaluation = {'final_test': test, 'splits': latest['splits'],
        'selected_alpha': latest['selected_spec']['alpha'], 'selected_spec': latest['selected_spec'],
        'tuning_trials': latest['tuning_trials'], 'evaluated_parameters': latest['model'],
        'evaluated_residual_interval_80': latest['model']['normalized_residual_interval_80'],
        'research_validation_passed': passed, 'production_approved': False,
        'gate_checks': checks, 'failed_checks': [k for k, ok in checks.items() if not ok],
        'outer_fold_wins': wins, 'outer_folds': folds,
        'direction_evidence_passed': passed and test['direction_advantage']['lower_95'] > 0,
        'evaluation_scope': 'Purged rolling historical reconstruction; latest 126 dates reused from prior research, not a new holdout.',
        'validation_reuse_warning': 'Historical tests have been inspected; only future frozen forecasts provide new prospective evidence.',
        'overlap_warning': 'Cross-stock rows and overlapping multi-day returns are dependent; paired bootstrap resamples date blocks.',
        'cost_assumption_bps': 20, 'cost_stress_bps': 50,
        'cost_assumption_note': 'Nonoverlapping full-liquidation baskets; no fills, price limits or impact simulation.',
        'knowledge_mode': 'public_reconstruction', 'protocol_version': VERSION,
        'release_policy': 'Only a validated research scenario may expose numerical returns; production approval is separate.'}
    return {'parameters': refreshed, 'evaluation': evaluation}
