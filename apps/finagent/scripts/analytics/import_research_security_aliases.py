#!/usr/bin/env python3
"""Resolve missing report identities with a saved official BSE mapping table.

No raw report codes, asset prices, listing dates or trading membership change.
The official table includes original NEEQ Select listing dates; these must not
be silently labelled BSE admission dates.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import lxml.html
from common import connect, jsonb

URL = 'https://www.bse.cn/service/code_mapping.html'
PILOT = {'833819', '830799', '831445', '430489', '839167', '834682'}


def parse_mapping(raw):
    tree = lxml.html.fromstring(raw.decode('utf-8'))
    records = {}
    for node in tree.xpath('//tr'):
        row = [c.text_content().strip() for c in node.xpath('./td')]
        if len(row) != 5 or not re.fullmatch(r'\d{6}', row[3]):
            continue
        number, name, listed, old, new = row
        if not re.fullmatch(r'920\d{3}', new) or not name:
            raise ValueError('Invalid official mapping row')
        year, month, day = map(int, listed.split('/'))
        if old in records:
            raise ValueError('Ambiguous old security code')
        records[old] = {'old_code': old, 'new_code': new, 'security_name': name,
                        'exchange_table_listing_date': dt.date(year, month, day).isoformat(),
                        'switch_date': '2025-05-06' if old in PILOT else '2025-10-09',
                        'official_row': row}
    if not records:
        raise ValueError('No official mapping rows')
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source-html', type=Path, required=True)
    ap.add_argument('--notices-json', type=Path, required=True)
    ap.add_argument('--scope-json', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    raw = args.source_html.read_bytes()
    mappings = parse_mapping(raw)
    scope = json.loads(args.scope_json.read_text())
    codes = sorted({r['stock_code'] for r in scope})
    notices = json.loads(args.notices_json.read_text())
    if {s['url'] for s in notices['sources']} != {
            'https://www.bse.cn/important_news/200025487.html',
            'https://www.bse.cn/important_news/200025603.html',
            'https://www.bse.cn/important_news/200026735.html'}:
        raise ValueError('Missing effective-date evidence')
    rows = [mappings[code] for code in codes]
    with connect(autocommit=False) as c:
        c.execute('SET TRANSACTION READ ONLY')
        assets = c.execute("SELECT * FROM market.assets WHERE symbol=ANY(%s) AND asset_type='stock'",
                           ([r['new_code'] for r in rows],)).fetchall()
    for row in rows:
        matches = [a for a in assets if a['symbol'] == row['new_code'] and a['exchange'] == 'BSE'
                   and a['name'] == row['security_name'] and not a['metadata'].get('identity_alias_of')]
        if len(matches) != 1:
            raise ValueError('Unconfirmed canonical identity: ' + row['old_code'])
        if any(s['stock_name'] != row['security_name'] for s in scope if s['stock_code'] == row['old_code']):
            raise ValueError('Report name contradicts official identity')
        row['asset_id'] = str(matches[0]['asset_id'])
        row['source_url'] = URL
        row['source_sha256'] = hashlib.sha256(raw).hexdigest()
    result = {'mappings': rows, 'scope_documents': len(scope), 'applied': False,
              'scope': 'research identity only; no price stitching or historical tradability assertions'}
    args.output.mkdir(exist_ok=True, parents=True)
    (args.output / 'IDENTITY_PLAN.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    if args.apply:
        with connect(admin=True, autocommit=False) as c:
            c.execute("SET LOCAL lock_timeout='5s'")
            c.execute("SET LOCAL statement_timeout='30s'")
            for row in rows:
                old = c.execute('SELECT * FROM market.research_security_code_aliases WHERE old_code=%s',
                                (row['old_code'],)).fetchone()
                if old and (str(old['asset_id']), str(old['switch_date']), old['source_sha256']) != (
                        row['asset_id'], row['switch_date'], row['source_sha256']):
                    raise ValueError('Existing identity evidence conflicts')
                c.execute('''INSERT INTO market.research_security_code_aliases
                    (old_code,asset_id,new_code,exchange,security_name,switch_date,exchange_table_listing_date,
                     source_url,source_sha256,evidence,observed_at)
                    VALUES(%s,%s,%s,'BSE',%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(old_code) DO NOTHING''',
                          (row['old_code'],row['asset_id'],row['new_code'],row['security_name'],
                           row['switch_date'],row['exchange_table_listing_date'],URL,row['source_sha256'],
                           jsonb({'mapping': row, 'notices': notices,
                                  'listing_date_scope': 'Official table includes former NEEQ Select dates; not a BSE-only admission date'}),
                           notices['observed_at']))
        result['applied'] = True
        (args.output / 'IDENTITY_APPLIED.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'aliases': len(rows), 'documents': len(scope), 'applied': result['applied']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
