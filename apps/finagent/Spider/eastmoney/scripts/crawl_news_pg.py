# -*- coding: utf-8 -*-
"""Eastmoney news crawl directly into Postgres corpus/news tables.

Usage:
  python scripts/crawl_news_pg.py --channel finance --limit 20
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eastmoney.fetchers import news
from eastmoney.pg_storage import PGStorage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", choices=sorted(news.CHANNELS), action="append")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    st = PGStorage()
    try:
        channels = args.channel or list(news.CHANNELS)
        for ch in channels:
            news.crawl(ch, st, limit=args.limit)
    finally:
        st.close()


if __name__ == "__main__":
    main()
