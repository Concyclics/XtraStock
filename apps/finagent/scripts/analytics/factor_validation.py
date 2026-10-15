"""Shared, versioned factor governance; no database or process side effects."""
from __future__ import annotations
import datetime as dt
import math

VALIDATION_VERSION = "purged-cost-pit-v3"

def independent_windows(rows, factor_version=None):
    """Greedy interval scheduling per horizon, including the label outcome period.

    Distinct but overlapping rolling tests never count as independent. Mixing
    horizons or factor versions does not create extra independent evidence.
    """
    by_horizon = {}
    for row in rows:
        m = row.get("metrics") or {}
        if not (row.get("status", row.get("run_status")) == "complete"
                and row.get("passed_gates") and m.get("validation_version") == VALIDATION_VERSION
                and m.get("input_contract_version") == "pit-input-v1" and m.get("strict_pit_ready") is True
                and m.get("cost_adjusted") and m.get("execution_constraints")
                and float(m.get("net_spread_mean") or 0) > 0
                and float(row.get("rank_ic_mean") or 0) > 0
                and (factor_version is None or m.get("factor_version") == factor_version)):
            continue
        start = dt.date.fromisoformat(str(row["test_start"])[:10])
        end = dt.date.fromisoformat(str(m.get("outcome_end") or row["test_end"])[:10])
        by_horizon.setdefault(int(row["horizon_days"]), []).append((start, end, row))
    selected = []
    for horizon, intervals in by_horizon.items():
        chosen = []
        last_end = dt.date.min
        for start, end, row in sorted(intervals, key=lambda x: (x[1], x[0])):
            if start > last_end:
                chosen.append(row); last_end = end
        if len(chosen) > len(selected):
            selected = chosen
    return selected

def hac_p_value(values, max_lag):
    """Bartlett/Newey-West mean standard error on daily cross-section ICs."""
    values = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    n = len(values)
    if n < max(10, 2 * max_lag + 2):
        return None
    mean = sum(values) / n
    residual = [v - mean for v in values]
    variance = sum(v*v for v in residual) / n
    for lag in range(1, min(max_lag, n-1) + 1):
        weight = 1 - lag / (max_lag + 1)
        covariance = sum(residual[i] * residual[i-lag] for i in range(lag, n)) / n
        variance += 2 * weight * covariance
    if variance <= 0:
        return None
    se = math.sqrt(variance / (n-1))
    return math.erfc(abs(mean) / se / math.sqrt(2))

def purged_folds(dates, folds=3, test_sessions=80, gap=20, min_train_sessions=500):
    """Frozen non-overlapping OOS blocks; purge an outcome horizon before each."""
    dates = sorted(set(dates))
    end = len(dates)
    result = []
    for _ in range(folds):
        start = end-test_sessions
        train_end = start-gap-1
        if train_end+1 < min_train_sessions:
            break
        result.append((dates[0], dates[train_end], dates[start], dates[end-1]))
        end = start-gap
    return list(reversed(result))
