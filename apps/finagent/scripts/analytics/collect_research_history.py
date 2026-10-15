"""Bounded, resumable public raw-price collection for historical research.

Preserves errors, delisted identities and source responses. It does not change
canonical prices or manufacture adjusted returns from negative QFQ prices.
The current security master is not a complete point-in-time historical universe.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import json
from pathlib import Path
import re
import signal
import time
import requests

ENDPOINT = 'https://web.ifzq.gtimg.cn/appstock/app/kline/kline'
CODE = re.compile(r'(?:60\d{4}|688\d{3}|00\d{4}|30\d{4})')


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temp.replace(path)


def years_before(day, years):
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def make_plan(master, size=256, seed='quant-history-v2', as_of=None, years=5):
    cutoff = date.fromisoformat(as_of) if as_of else datetime.now(timezone.utc).date()
    since = years_before(cutoff, years)
    eligible = [a for a in master if CODE.fullmatch(a['symbol']) and a['exchange'] in ('SSE', 'SZSE')]
    old = [a for a in eligible if (not a.get('listed_date') or a['listed_date'] <= str(cutoff))
           and a.get('lifecycle_status') != 'delisted' and not a.get('has_delisting_evidence')]
    old.sort(key=lambda a: hashlib.sha256((seed + ':' + a['symbol']).encode()).hexdigest())
    # Keep all known pre-2020 delistings, including unknown IPO dates. Excluding
    # them for incomplete metadata would strengthen survivorship bias.
    dead = [a for a in eligible if (a.get('lifecycle_status') == 'delisted' or a.get('delisted_date') or a.get('has_delisting_evidence'))
            and (not a.get('listed_date') or a['listed_date'] <= str(cutoff))
            and (not a.get('delisted_date') or a['delisted_date'] >= str(since))]
    dead.sort(key=lambda a: (a['exchange'], a['symbol']))
    selected = {a['symbol']: a for a in [*old[:size], *dead]}
    windows = []
    cursor = since
    while cursor <= cutoff:
        end = min(years_before(cursor, -2) - timedelta(days=1), cutoff)
        windows.append((str(cursor), str(end)))
        cursor = end + timedelta(days=1)
    jobs = []
    for symbol, asset in sorted(selected.items()):
        provider_symbol = ('sh' if asset['exchange'] == 'SSE' else 'sz') + symbol
        for start, end in windows:
            if asset.get('listed_date') and asset['listed_date'] > end:
                continue
            if asset.get('delisted_date') and asset['delisted_date'] < start:
                continue
            jobs.append({'symbol': symbol, 'provider_symbol': provider_symbol, 'start': start, 'end': end})
    return {'version': 'raw-history-v2', 'seed': seed, 'active_sample_size': size,
            'primary_since': str(since), 'as_of': str(cutoff), 'primary_years': years,
            'available_active_candidates': len(old), 'delisting_evidence_included': len(dead),
            'securities': list(selected.values()), 'jobs': jobs, 'source_url': ENDPOINT,
            'adjustment': 'none', 'volume_unit': 'provider lots; must cross-check',
            'historical_universe_complete': False, 'corporate_actions_complete': False,
            'limitations': ['Current master misses some former securities and IPO dates.',
                            'Raw prices alone cannot establish total investment returns.',
                            'Empty responses and truncated histories are unresolved data gaps.']}


def parse_response(value, job):
    if not isinstance(value, dict) or value.get('code') != 0 or not isinstance(value.get('data'), dict):
        raise ValueError('provider_error_response')
    series = value['data'].get(job['provider_symbol'], {})
    rows = series.get('day', []) if isinstance(series, dict) else []
    if not isinstance(rows, list):
        raise ValueError('provider_rows_not_list')
    normalized = []
    seen = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise ValueError('malformed_price_row')
        day = row[0]
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or not job['start'] <= day <= job['end']:
            raise ValueError('out_of_range_date')
        if day in seen:
            raise ValueError('duplicate_date')
        seen.add(day)
        opened, close, high, low, volume = map(float, row[1:6])
        if not (0 < low <= min(opened, close) <= max(opened, close) <= high < float('inf')
                and 0 <= volume < float('inf')):
            raise ValueError('invalid_ohlcv')
        normalized.append({'date': day, 'symbol': job['symbol'], 'open': opened, 'close': close,
                            'high': high, 'low': low, 'volume': volume})
    if [r['date'] for r in normalized] != sorted(seen):
        raise ValueError('nonchronological_dates')
    if len(normalized) >= 640:
        raise ValueError('possible_response_truncation')
    return normalized


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--master', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sample-size', type=int, default=256)
    parser.add_argument('--interval', type=float, default=.5)
    parser.add_argument('--as-of', help='Last requested calendar date; defaults to today UTC.')
    parser.add_argument('--years', type=int, default=5, choices=range(1, 6), help='Primary recent-data window; maximum five years.')
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.sample_size <= 2000 or args.interval < .25:
        raise SystemExit('Invalid collection bounds')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    raw_dir = out / 'historical-source'
    raw_dir.mkdir(exist_ok=True)
    lock = (out / 'collector.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    plan = make_plan(json.loads(args.master.read_text()), args.sample_size, as_of=args.as_of, years=args.years)
    plan['master_sha256'] = hashlib.sha256(args.master.read_bytes()).hexdigest()
    plan['collector_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    plan_path = out / 'COLLECTION_PLAN.json'
    if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
        raise SystemExit('Master, code or plan changed; choose a new output directory')
    save(plan_path, plan)
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    session = requests.Session()
    session.headers['User-Agent'] = 'Mozilla/5.0'
    receipts = []
    began = datetime.now(timezone.utc).isoformat()

    def progress(status):
        result = {'status': status, 'started_at': began, 'updated_at': datetime.now(timezone.utc).isoformat(),
                  'jobs_planned': len(plan['jobs']), 'jobs_recorded': len(receipts),
                  'successful_nonempty': sum(r['status'] == 'received' for r in receipts),
                  'empty_responses': sum(r['status'] == 'empty' for r in receipts),
                  'failed_responses': sum(r['status'] == 'error' for r in receipts),
                  'rows_received': sum(r.get('rows', 0) for r in receipts),
                  'historical_universe_complete': False, 'total_returns_verified': False}
        save(out / 'PROGRESS.json', result)
        print(json.dumps(result), flush=True)
        return result

    try:
        for job in plan['jobs']:
            if stopped:
                break
            stem = f"{job['provider_symbol']}-{job['start'][:4]}-{job['end'][:4]}"
            receipt_path = raw_dir / (stem + '.receipt.json')
            archive = raw_dir / (stem + '.json.gz')
            if receipt_path.exists():
                cached = json.loads(receipt_path.read_text())
                valid_cache = archive.exists() and hashlib.sha256(archive.read_bytes()).hexdigest() == cached.get('archive_sha256')
                if valid_cache and not (args.retry_failed and cached['status'] == 'error'):
                    receipts.append(cached)
                    continue
                if archive.exists() and not valid_cache:
                    raise ValueError('Existing source archive does not match its checksum')
            receipt = {**job, 'status': 'error', 'retrieved_at': datetime.now(timezone.utc).isoformat()}
            value = None
            try:
                response = session.get(ENDPOINT, params={'param': f"{job['provider_symbol']},day,{job['start']},{job['end']},640"}, timeout=(5, 15))
                receipt['http_status'] = response.status_code
                receipt['response_sha256'] = hashlib.sha256(response.content).hexdigest()
                receipt['response_bytes'] = len(response.content)
                value = {'response_text': response.text}
                response.raise_for_status()
                decoded = response.json()
                value['decoded'] = decoded
                rows = parse_response(decoded, job)
                value['normalized_raw_bars'] = rows
                receipt.update(status='received' if rows else 'empty', rows=len(rows),
                               first=rows[0]['date'] if rows else None, last=rows[-1]['date'] if rows else None)
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                receipt.update(error_type=type(exc).__name__, error=str(exc)[:200])
            # Preserve previous failed responses when an explicit retry is made.
            if archive.exists():
                stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
                archive.rename(raw_dir / (stem + '.prior-' + stamp + '.json.gz'))
                receipt_path.rename(raw_dir / (stem + '.prior-' + stamp + '.receipt.json'))
            temp = archive.with_suffix('.tmp')
            with gzip.open(temp, 'wt') as stream:
                json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            temp.replace(archive)
            receipt['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
            save(receipt_path, receipt)
            receipts.append(receipt)
            if len(receipts) % 25 == 0:
                progress('running')
            time.sleep(args.interval)
        status = 'stopped' if stopped else ('complete_with_gaps' if any(r['status'] != 'received' for r in receipts) else 'collection_complete')
        summary = progress(status)
        save(out / 'COLLECTION_RECEIPTS.json', receipts)
        if not stopped:
            save(out / 'COMPLETE.json', summary)
    finally:
        session.close()
        lock.close()


if __name__ == '__main__':
    main()
