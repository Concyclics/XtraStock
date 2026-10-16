"""Build auditable financial feature panels from a frozen local SQL export."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from financial_history import CONTRACT, FEATURE_NAMES, MODES, VALUE_NAMES, build_feature_panel, normalize_event
from strategy_dataset import checksum, dump
from strategy_research import load_dataset, prepare_features


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-age-days', type=int, default=550)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = json.loads((args.source/'MANIFEST.json').read_text())
    market = json.loads((args.dataset/'manifest.json').read_text())
    if source['status'] != 'complete' or source['primary_since'] != market['primary_since'] or source['until'] != market['until']:
        raise ValueError('Source completion/window differs from the frozen market panel')
    for name, expected in source['sha256'].items():
        if checksum(args.source/name) != expected:
            raise ValueError('Frozen financial source changed: '+name)
    if (source['asset_catalog_sha256'] != checksum(args.dataset/'assets.json')
            or source['market_sha256'] != checksum(args.dataset/'market.npz')):
        raise ValueError('Source security identities or market panel changed')
    data = load_dataset(args.dataset, 'all')
    features = prepare_features(data)
    assets = {str(a['asset_id']): a for a in data['assets']}
    accepted, rejected, reasons, event_ids = [], 0, Counter(), set()
    with gzip.open(args.source/'events.jsonl.gz', 'rt') as stream, gzip.open(args.output/'rejected.jsonl.gz', 'wt') as bad:
        for line in stream:
            row = json.loads(line)
            if row['id'] in event_ids:
                raise ValueError('Duplicate source event identity')
            event_ids.add(row['id'])
            try:
                accepted.append(normalize_event(row, assets[str(row['asset_id'])]))
            except (ValueError, KeyError, TypeError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                bad.write(json.dumps({'event_id': row['id'], 'reason': reason})+'\n')
                rejected += 1
                reasons[reason] += 1
    if len(event_ids) != source['rows'] or not accepted:
        raise ValueError('Source count mismatch or no usable financial records')
    with gzip.open(args.output/'accepted.jsonl.gz', 'wt') as output:
        for row in accepted:
            output.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
    primary = data['dates'] >= market['primary_since']
    years = np.array([str(d)[:4] for d in data['dates']])
    audits, coverage = [], []
    for mode in MODES:
        values, selected, audit = build_feature_panel(accepted, data['assets'], data['dates'], mode=mode, max_age_days=args.max_age_days)
        path = args.output/(mode+'.npz')
        np.savez_compressed(path, dates=data['dates'], symbols=data['symbols'], feature_names=np.array(FEATURE_NAMES),
                            values=values, source_event_ids=selected)
        audits.append(audit)
        for year in sorted(set(years[primary])):
            mask = primary & (years == year)
            eligible = features['eligible'][mask]
            denominator = int(eligible.sum())
            for j, name in enumerate(VALUE_NAMES):
                visible = np.isfinite(values[mask, :, j]) & eligible
                coverage.append({'model': name, 'allocation': mode, 'cohort': 'all', 'year': year,
                    'eligible_asset_days': denominator, 'available_asset_days': int(visible.sum()),
                    'coverage_fraction': float(visible.sum()/denominator) if denominator else None,
                    'covered_securities': int(visible.any(axis=0).sum()), 'trading_sessions': int(mask.sum()),
                    'production_approved': False})
        print(json.dumps({'mode': mode, **audit}), flush=True)
        del values, selected
    pd.DataFrame(coverage).to_csv(args.output/'COVERAGE_RESULTS.csv', index=False)
    delay = np.array([r['publication_delay_days'] for r in accepted])
    code = args.output/'code-at-execution'
    code.mkdir()
    code_names = ('financial_history.py', 'build_financial_history_panel.py', 'verify_financial_history_panel.py', 'strategy_research.py', 'strategy_dataset.py')
    for name in code_names:
        shutil.copyfile(Path(__file__).with_name(name), code/name)
    manifest = {'status': 'complete', 'created_at': datetime.now(timezone.utc).isoformat(), 'contract': CONTRACT,
        'primary_since': market['primary_since'], 'until': market['until'], 'feature_data_since': market['feature_data_since'],
        'source_manifest_sha256': checksum(args.source/'MANIFEST.json'), 'source_archive_sha256': source['sha256']['events.jsonl.gz'],
        'market_sha256': source['market_sha256'], 'asset_catalog_sha256': source['asset_catalog_sha256'],
        'source_rows': source['rows'], 'accepted_rows': len(accepted), 'rejected_rows': rejected, 'rejected_reasons': dict(reasons),
        'features': list(FEATURE_NAMES), 'max_age_days': args.max_age_days, 'audits': audits,
        'revision_delayed_records': int((delay > 0).sum()), 'median_publication_delay_days': float(np.median(delay)),
        'source_cash_per_share_records': sum(r['cash_per_share_present'] for r in accepted),
        'cash_feature_status': 'not_modeled: per-share cash and book denominators are not certified to use identical share bases',
        'sha256': {p.name: checksum(p) for p in args.output.iterdir() if p.is_file()},
        'code': {name: checksum(code/name) for name in code_names},
        'methodology': [
            'Latest visible financial period and revision at 15:00 Asia/Shanghai; source notice/update/EITIME dates conservatively lag one calendar day.',
            'Public revision reconstruction preserves supplier update dates but is not an archived historical system record. System observation additionally requires actual ingestion.',
            'Same-clock conflicting values invalidate that period; a missing field never borrows from another report. Staleness starts at period end.',
            'Reported parent-profit margin is not consolidated net margin. ROE is reported year-to-date with period-month context, not silently annualized.',
            'Gross margin, cumulative ROE and reported growth are converted from percentage points to fractions. QMJ and gross-profit-to-assets strategies are not replicated.',
            'Only the primary five-year window can supply training labels or scored returns. Earlier rows are feature warmup only.',
        ], 'production_approved': False, 'model_trained': False}
    dump(args.output/'MANIFEST.json', manifest)
    print(json.dumps({k: manifest[k] for k in ['status', 'accepted_rows', 'rejected_rows', 'revision_delayed_records', 'median_publication_delay_days']}), flush=True)


if __name__ == '__main__':
    main()
