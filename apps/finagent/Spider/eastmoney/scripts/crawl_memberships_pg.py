# -*- coding: utf-8 -*-
"""Crawl Eastmoney board constituents into market.asset_memberships.

Usage:
  python scripts/crawl_memberships_pg.py --asset-type industry --limit 10
  python scripts/crawl_memberships_pg.py --asset-type industry --asset-type concept
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eastmoney.fetchers import boards
from eastmoney.pg_storage import PGStorage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset-type", action="append", choices=["industry", "concept", "index"], default=None)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    st = PGStorage()
    try:
        wanted = set(args.asset_type or ["industry", "concept"])
        parents = [r for r in st.list_instruments() if r.get("type") in wanted]
        if args.limit:
            parents = parents[:args.limit]
        total = 0
        for parent in parents:
            code = parent["code"]
            secid = parent["secid"]
            typ = parent["type"]
            rows = boards.fetch_clist(f"b:{code}")
            n = st.upsert_memberships(secid, rows, typ)
            total += n
            print(f"[memberships_pg] {typ} {secid} {parent.get('name')}: fetched={len(rows)} upsert={n}")
        print(f"[memberships_pg] total={total}")
    finally:
        st.close()


if __name__ == "__main__":
    main()
