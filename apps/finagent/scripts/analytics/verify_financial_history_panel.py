"""Independently reconstruct financial selections with per-period as-of arrays."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NAMES = ('reported_gross_margin', 'parent_profit_to_revenue', 'reported_ytd_roe',
         'reported_revenue_growth_yoy', 'reported_parent_profit_growth_yoy')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def jsonlines(path):
    with gzip.open(path, 'rt') as stream:
        return [json.loads(line) for line in stream]


def check_source_values(raw, normalized, asset):
    """Compare accepted values and both clocks directly with the raw payload."""
    r = raw['raw_payload']
    assert str(raw['asset_id']) == normalized['asset_id'] == str(asset['asset_id'])
    assert raw['source'] == 'eastmoney:datacenter'
    assert r['SECURITY_CODE'] == asset['symbol']
    assert r['SECUCODE'] == asset['symbol']+'.'+{'SSE': 'SH', 'SZSE': 'SZ', 'BSE': 'BJ'}[asset['exchange']]
    assert str(raw['event_date']) == str(r['REPORTDATE'])[:10] == normalized['period_end']
    period = date.fromisoformat(normalized['period_end'])
    assert (period.month, period.day) in ((3, 31), (6, 30), (9, 30), (12, 31))
    dated = [date.fromisoformat(r[key][:10]) for key in ('NOTICE_DATE', 'UPDATE_DATE')]
    assert dated[0] > period
    if r.get('EITIME'):
        dated.append(date.fromisoformat(r['EITIME'][:10]))
    public = pd.Timestamp(datetime.combine(max(dated)+timedelta(days=1), time(), ZoneInfo('Asia/Shanghai')))
    assert pd.Timestamp(normalized['public_at']) == public
    created = pd.Timestamp(raw['created_at'])
    assert created.tzinfo is not None and pd.Timestamp(normalized['observed_at']) == max(public, created)

    def n(key):
        value = r.get(key)
        if isinstance(value, bool):
            return math.nan
        try:
            return float(value) if math.isfinite(float(value)) else math.nan
        except (TypeError, ValueError, OverflowError):
            return math.nan
    revenue = n('TOTAL_OPERATE_INCOME')
    expected = [n('XSMLL')*.01, n('PARENT_NETPROFIT')/revenue if revenue > 0 else math.nan,
                n('WEIGHTAVG_ROE')*.01, n('YSTZ')*.01, n('SJLTZ')*.01]
    for key, value in zip(NAMES, expected):
        actual = normalized['values'][key]
        if not math.isfinite(value) or abs(value) > np.finfo(np.float32).max:
            assert actual is None
        else:
            assert actual is not None and math.isclose(actual, value, rel_tol=2e-15, abs_tol=1e-15)
    assert normalized['period_months'] == period.month


def reference_panel(events, assets, dates, mode, max_age_days):
    """Vectorized per-period last-version lookup, independent of the daily loop."""
    if mode not in ('public_revision_reconstruction', 'system_observed'):
        raise ValueError('unsupported audit clock')
    clock = 'public_at' if mode == 'public_revision_reconstruction' else 'observed_at'
    by_asset = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for row in events:
        by_asset[row['asset_id']][row['period_end']][pd.Timestamp(row[clock]).value].append(row)
    days = pd.to_datetime(np.asarray(dates))
    cutoffs = (days+pd.Timedelta(hours=15)).tz_localize('Asia/Shanghai').asi8
    output = np.full((len(days), len(assets), len(NAMES)+2), np.nan, dtype=np.float32)
    ids = np.full(output.shape[:2], -1, dtype=np.int64)
    for j, asset in enumerate(assets):
        for period, times in sorted(by_asset[str(asset['asset_id'])].items()):
            stamps, choices = [], []
            for stamp, rows in sorted(times.items()):
                fingerprints = {tuple(r['values'][k] for k in NAMES) for r in rows}
                stamps.append(stamp)
                choices.append(None if len(fingerprints) != 1 else min(rows, key=lambda r: r['event_id']))
            positions = np.searchsorted(np.asarray(stamps, dtype=np.int64), cutoffs, side='right')-1
            visible = positions >= 0
            # Overwrite even with missing/conflicted values: this is now the
            # most recent visible financial period, not an older fallback.
            output[visible, j] = np.nan
            ids[visible, j] = -1
            age = (days-pd.Timestamp(period)).days.to_numpy()
            for version, row in enumerate(choices):
                if row is None:
                    continue
                use = (positions == version) & (age >= 0) & (age <= max_age_days)
                if not use.any():
                    continue
                output[use, j, :len(NAMES)] = [np.nan if row['values'][k] is None else row['values'][k] for k in NAMES]
                output[use, j, -2] = date.fromisoformat(period).month
                output[use, j, -1] = age[use]
                ids[use, j] = row['event_id']
    return output, ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--panel', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((args.panel/'MANIFEST.json').read_text())
    assert manifest['status'] == 'complete'
    assert digest(args.source/'events.jsonl.gz') == manifest['source_archive_sha256']
    assert digest(args.source/'MANIFEST.json') == manifest['source_manifest_sha256']
    assert digest(args.dataset/'market.npz') == manifest['market_sha256']
    assert digest(args.dataset/'assets.json') == manifest['asset_catalog_sha256']
    for name, value in manifest['sha256'].items():
        assert digest(args.panel/name) == value
    for name, value in manifest['code'].items():
        assert digest(args.panel/'code-at-execution'/name) == value == digest(Path(__file__).with_name(name))
    assets = json.loads((args.dataset/'assets.json').read_text())
    by_asset = {str(a['asset_id']): a for a in assets}
    raw_rows = jsonlines(args.source/'events.jsonl.gz')
    raw = {r['id']: r for r in raw_rows}
    accepted, rejected = jsonlines(args.panel/'accepted.jsonl.gz'), jsonlines(args.panel/'rejected.jsonl.gz')
    accepted_ids, rejected_ids = [r['event_id'] for r in accepted], [r['event_id'] for r in rejected]
    assert len(raw) == len(raw_rows) == manifest['source_rows']
    assert len(set(accepted_ids)) == len(accepted_ids) == manifest['accepted_rows']
    assert len(set(rejected_ids)) == len(rejected_ids) == manifest['rejected_rows']
    assert not set(accepted_ids).intersection(rejected_ids)
    assert set(accepted_ids).union(rejected_ids) == set(raw)
    for row in accepted:
        check_source_values(raw[row['event_id']], row, by_asset[row['asset_id']])
    del raw, raw_rows
    from strategy_research import load_dataset, prepare_features
    market = load_dataset(args.dataset, 'all')
    eligible = prepare_features(market)['eligible']
    years = np.array([str(d)[:4] for d in market['dates']])
    primary = market['dates'] >= manifest['primary_since']
    table = pd.read_csv(args.panel/'COVERAGE_RESULTS.csv')
    checks = []
    for mode in ('public_revision_reconstruction', 'system_observed'):
        with np.load(args.panel/(mode+'.npz'), allow_pickle=False) as loaded:
            assert np.array_equal(loaded['dates'], market['dates'])
            assert np.array_equal(loaded['symbols'], market['symbols'])
            assert loaded['feature_names'].tolist() == [*NAMES, 'reported_period_months', 'report_age_days']
            expected, ids = reference_panel(accepted, assets, loaded['dates'], mode, manifest['max_age_days'])
            np.testing.assert_equal(loaded['source_event_ids'], ids)
            np.testing.assert_equal(loaded['values'], expected)
            for row in table[table.allocation == mode].to_dict('records'):
                mask = primary & (years == str(row['year']))
                valid = np.isfinite(expected[mask, :, NAMES.index(row['model'])]) & eligible[mask]
                assert int(valid.sum()) == row['available_asset_days']
                assert int(eligible[mask].sum()) == row['eligible_asset_days']
                assert int(valid.any(axis=0).sum()) == row['covered_securities']
                if row['eligible_asset_days']:
                    assert abs(row['coverage_fraction']-valid.sum()/eligible[mask].sum()) < 1e-14
            checks.append({'mode': mode, 'selected_asset_days': int((ids >= 0).sum()),
                           'all_source_ids_identical': True, 'all_feature_values_identical': True})
        del expected, ids
    args.report.write_text(json.dumps({'status': 'verified', 'accepted_source_rows': len(accepted),
        'rejected_source_rows_accounted': len(rejected), 'coverage_rows': len(table), 'checks': checks,
        'verifier_sha256': digest(Path(__file__)), 'production_approved': False}, indent=2)+'\n')
    print(json.dumps({'status': 'verified', 'source_rows': manifest['source_rows'], 'coverage_rows': len(table), 'checks': checks}))


if __name__ == '__main__':
    main()
