"""Attribute declared account windows; never refit models or place trades."""
from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path

import pandas as pd

from nav_attribution import attribute_daily, summarize_window
from strategy_dataset import checksum, dump
from strategy_research import load_dataset


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('dataset', 'accounts', 'manifest', 'output'):
        p.add_argument('--'+name, type=Path, required=True)
    args = p.parse_args(); manifest = json.loads(args.manifest.read_text())
    out = args.output.resolve()
    if out.exists():
        raise ValueError('Attribution output must be a new directory')
    for name, expected in manifest['market_source'].items():
        if checksum(args.dataset/name) != expected:
            raise ValueError('Declared market input differs: '+name)
    for row in manifest['accounts']:
        if checksum(args.accounts/row['source']) != row['source_sha256']:
            raise ValueError('Declared account differs: '+row['source'])
    data = load_dataset(args.dataset, 'all'); out.mkdir(parents=True)
    summary, audits, losses = [], [], []
    for number, row in enumerate(manifest['accounts']):
        with gzip.open(args.accounts/row['source'], 'rt') as stream: result = json.load(stream)
        detail = attribute_daily(result, data)
        identity = {k: row[k] for k in ('model', 'allocation', 'cohort', 'cost', 'initial_cash')}
        identity.update(horizon=20, production_approved=False)
        with gzip.open(out/f'account-{number}.json.gz', 'wt', encoding='utf-8') as stream:
            json.dump(detail, stream, allow_nan=False)
        audits.append({**identity, **{k: v for k, v in detail.items() if k not in ('daily', 'asset_days')}})
        for window in manifest['windows']:
            selected = summarize_window(detail, window['start'], window['end'])
            common = {**identity, 'window': window['id'], 'year': window['year']}
            summary.append({**common, **{k: v for k, v in selected.items() if k != 'asset_contributions'}})
            losses.extend({**common, 'loss_rank': i+1, **r} for i, r in enumerate(selected['asset_contributions'][:10]))
        print(json.dumps({'account': number+1, 'status': detail['status'], 'sessions': detail['sessions']}), flush=True)
    for name, rows in [('WINDOW_ATTRIBUTION.csv', summary), ('ATTRIBUTION_AUDIT.csv', audits), ('WINDOW_LOSS_CONTRIBUTORS.csv', losses)]:
        pd.DataFrame(rows).to_csv(out/name, index=False)
    dump(out/'COMPLETE.json', {'status': 'daily_attribution_verified', 'accounts': len(audits),
        'sessions': sum(r['sessions'] for r in audits), 'windows': len(summary), 'manifest_sha256': checksum(args.manifest),
        'maximum_nav_or_pnl_error_cny': max(r['maximum_nav_or_pnl_error_cny'] for r in audits),
        'code': {name: checksum(Path(__file__).with_name(name)) for name in ('nav_attribution.py', 'run_nav_attribution.py')},
        'artifacts': {p.name: checksum(p) for p in out.iterdir()}, 'production_approved': False})


if __name__ == '__main__':
    main()
