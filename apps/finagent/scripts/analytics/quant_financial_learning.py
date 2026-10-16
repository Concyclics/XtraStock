"""Controlled financial additions to an unchanged market/EPS feature matrix."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from financial_history import FEATURE_NAMES, VALUE_NAMES
from horizon_signals import validate_study as validate_horizons
from strategy_dataset import checksum
from strategy_research import rank


GROUPS = {
    'profitability': tuple(VALUE_NAMES[:3]),
    'growth': tuple(VALUE_NAMES[3:]),
    'combined': tuple(VALUE_NAMES),
}
MODE = 'public_revision_reconstruction'


def validate_study(study):
    validate_horizons(study)
    expected = {'annual_' + group: list(values) for group, values in GROUPS.items()}
    if (study['financial_groups'] != expected or set(study['variants']) != set(expected)
            or study['label_horizon'] != 20 or study['validation_sessions'] != 63
            or study['prediction_reference_map'] != {'annual_tree_blend': 'annual_tree_blend'}
            or study['score_blends'] or study['financial_clock'] != MODE
            or study['context_scales'] != {'reported_period_months': 12, 'report_age_days': 550}
            or study['baseline_features'] != 72):
        raise ValueError('Financial ablation differs from its controlled feature contract')
    for config in study['variants'].values():
        if config != {'family': 'tree_blend', 'cadence': 'annual', 'half_life_sessions': None}:
            raise ValueError('Financial ablation cannot change the model family or training cadence')


def load_verified_panel(directory, verification, dataset, data):
    directory, verification, dataset = map(Path, (directory, verification, dataset))
    manifest = json.loads((directory/'MANIFEST.json').read_text())
    report = json.loads(verification.read_text())
    market = json.loads((dataset/'manifest.json').read_text())
    if (manifest['status'] != 'complete' or report['status'] != 'verified'
            or manifest['features'] != list(FEATURE_NAMES) or manifest['max_age_days'] != 550
            or manifest['market_sha256'] != checksum(dataset/'market.npz')
            or manifest['asset_catalog_sha256'] != checksum(dataset/'assets.json')
            or any(manifest[key] != market[key] for key in ('primary_since', 'until', 'feature_data_since'))
            or report['accepted_source_rows'] != manifest['accepted_rows']
            or report['rejected_source_rows_accounted'] != manifest['rejected_rows']
            or report['verifier_sha256'] != manifest['code']['verify_financial_history_panel.py']):
        raise ValueError('Financial source identity, window or independent verification differs')
    checks = report['checks']
    if ({c['mode'] for c in checks} != {MODE, 'system_observed'} or len(checks) != 2
            or not all(c['all_source_ids_identical'] and c['all_feature_values_identical'] for c in checks)):
        raise ValueError('Financial source verification is incomplete')
    for name, digest in manifest['sha256'].items():
        if checksum(directory/name) != digest:
            raise ValueError('Financial panel changed: ' + name)
    for name, digest in manifest['code'].items():
        if checksum(directory/'code-at-execution'/name) != digest:
            raise ValueError('Financial construction code changed: ' + name)
    with np.load(directory/(MODE+'.npz'), allow_pickle=False) as archive:
        if (not np.array_equal(archive['dates'], data['dates'])
                or not np.array_equal(archive['symbols'], data['symbols'])
                or not np.array_equal(archive['feature_names'], FEATURE_NAMES)):
            raise ValueError('Financial panel axes differ from the frozen market')
        values = archive['values']
        selected = archive['source_event_ids']
    if (values.shape != (*data['close'].shape, len(FEATURE_NAMES))
            or selected.shape != data['close'].shape or np.isinf(values).any()
            or not np.array_equal(np.isfinite(values[..., -1]), selected >= 0)
            or np.any(np.isfinite(values[..., :5]).any(axis=-1) & (selected < 0))
            or not np.all(np.isin(values[..., -2][selected >= 0], [3, 6, 9, 12]))
            or np.any((values[..., -1][selected >= 0] < 0) | (values[..., -1][selected >= 0] > 550))):
        raise ValueError('Financial values or source observation masks are invalid')
    return values, {
        'mode': MODE, 'manifest_sha256': checksum(directory/'MANIFEST.json'),
        'verification_sha256': checksum(verification), 'panel_sha256': manifest['sha256'][MODE+'.npz'],
        'source_archive_sha256': manifest['source_archive_sha256'], 'max_age_days': manifest['max_age_days'],
        'public_reconstruction_not_system_history': True,
    }


def append_financial_features(base, names, eligible, values, group):
    """Daily cross-sectional ranks; no backward fills or zero-valued observations.

    Eligibility is the original market eligibility, independent of financial
    coverage. Tree missing-value handling and training-only ridge imputation
    remain in the existing fitters. Context is scaled, never annualized.
    """
    group = tuple(group)
    if group not in GROUPS.values():
        raise ValueError('Unknown financial feature group')
    if (base.ndim != 3 or base.shape[:2] != eligible.shape or len(names) != base.shape[-1]
            or values.shape != (*eligible.shape, len(FEATURE_NAMES))):
        raise ValueError('Feature dimensions differ')
    columns, added = [], []
    for name in group:
        raw = values[..., FEATURE_NAMES.index(name)]
        available = eligible & np.isfinite(raw)
        columns.extend([np.where(available, rank(raw, available), np.nan), available.astype(float)])
        added.extend([name+'_rank', name+'_available'])
    for name, scale in [('reported_period_months', 12), ('report_age_days', 550)]:
        raw = values[..., FEATURE_NAMES.index(name)]
        available = eligible & np.isfinite(raw)
        columns.extend([np.where(available, raw/scale, np.nan), available.astype(float)])
        added.extend([name+'_scaled', name+'_available'])
    if set(names) & set(added) or len(set(names)) != len(names):
        raise ValueError('Duplicate feature names')
    extras = np.stack(columns, axis=-1).astype(base.dtype)
    return np.concatenate([base, extras], axis=-1), [*names, *added]
