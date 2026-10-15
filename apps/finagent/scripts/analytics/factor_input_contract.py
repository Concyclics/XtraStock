"""Shared factor input policy. Pure functions; no database or service effects."""
from __future__ import annotations
import math

INPUT_CONTRACT_VERSION = 'pit-input-v1'


def require_stock_horizon(horizon: int) -> None:
    if horizon not in (5, 20, 60):
        raise ValueError('Stock research supports 5/20/60-session holdings; next-open to same-close 1d is diagnostic only under T+1.')


def eligible_mining_fields(stats: dict, min_coverage: float = .60) -> tuple[list[str], dict[str, str]]:
    """Require observed input and variation before spending candidate budget."""
    ready, skipped = [], {}
    for field, row in stats.items():
        n, total = int(row.get('observed') or 0), int(row.get('total') or 0)
        variance = row.get('stddev')
        if not total or n / total < min_coverage:
            skipped[field] = f'current input coverage {n}/{total} below {min_coverage:.0%}'
        elif variance is None or not math.isfinite(float(variance)) or float(variance) <= 0:
            skipped[field] = 'current input has no finite cross-sectional variation'
        else:
            ready.append(field)
    return ready, skipped
