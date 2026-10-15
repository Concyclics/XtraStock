"""Versioned primary-source method cards, separate from measured strategy results."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from psycopg.types.json import Jsonb

REQUIRED = {'method_key', 'title', 'source', 'finding', 'adaptation', 'required_data', 'tests', 'limitations', 'tags'}


def validate_cards(value):
    if value.get('schema_version') != 1 or not isinstance(value.get('methods'), list):
        raise ValueError('unsupported method library format')
    seen = set()
    for card in value['methods']:
        if not REQUIRED <= card.keys() or card['method_key'] in seen:
            raise ValueError('incomplete or duplicate method')
        source = card['source']
        if not {'title', 'authors', 'year', 'url', 'scope'} <= source.keys():
            raise ValueError('incomplete primary-source provenance')
        url = urlparse(source['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('invalid source URL')
        if not card['required_data'] or not card['tests'] or not card['limitations']:
            raise ValueError('method lacks data requirements, evaluation or limits')
        seen.add(card['method_key'])
    return value['methods']


def register_methods(conn, value):
    cards = validate_cards(value)
    inserted = 0
    for card in cards:
        digest = hashlib.sha256(json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        row = conn.execute('''INSERT INTO research.quant_method_versions
          (method_key,title,source_url,content_sha256,payload) VALUES(%s,%s,%s,%s,%s)
          ON CONFLICT(method_key,content_sha256) DO NOTHING RETURNING method_version_id''',
          (card['method_key'], card['title'], card['source']['url'], digest, Jsonb(card))).fetchone()
        inserted += row is not None
    return {'methods': len(cards), 'inserted': inserted, 'unchanged': len(cards)-inserted}


def load_methods(conn, cutoff, limit=10, query=None):
    if cutoff.tzinfo is None:
        raise ValueError('knowledge cutoff must include timezone')
    query = str(query or '').strip()[:120]
    # Treat %, _ and backslash as literal query text, not wildcard controls.
    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    rows = conn.execute('''WITH latest AS (
        SELECT DISTINCT ON(method_key) method_key,payload,registered_at
        FROM research.quant_method_versions WHERE registered_at<=%s
        ORDER BY method_key,registered_at DESC,method_version_id DESC)
      SELECT * FROM latest WHERE %s='' OR payload::text ILIKE %s
      ORDER BY method_key LIMIT %s''', (cutoff, query, '%' + escaped + '%', max(1, min(20, int(limit))))).fetchall()
    return [{'component': 'quant_research_method', 'entity_key': row['method_key'],
             'available_at': row['registered_at'], **row['payload'],
             'evidence_kind': 'external_primary_research', 'prediction_eligible': False}
            for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    value = json.loads(args.catalog.read_text())
    cards = validate_cards(value)
    if args.check:
        print(json.dumps({'valid_methods': len(cards)}))
        return
    from common import connect
    with connect(autocommit=False, attempts=1) as conn:
        conn.execute("SET LOCAL statement_timeout='10s'")
        result = register_methods(conn, value)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
