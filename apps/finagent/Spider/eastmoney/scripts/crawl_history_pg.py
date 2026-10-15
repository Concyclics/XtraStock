# -*- coding: utf-8 -*-
"""Historical Eastmoney backfill directly into Postgres.

Usage:
  python scripts/crawl_history_pg.py --secid 1.600909 --macro
  python scripts/crawl_history_pg.py --universe --limit 100
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tqdm

from config import apiConfig, systemConfig
from eastmoney.pg_storage import PGStorage
from eastmoney.fetchers import kline, fundflow, fundamental, macro, events


EVENT_CATS = ["dragon_tiger", "block_trade", "margin_trade", "unlock", "survey", "northbound"]


def load_pg_universe(asset_types=None):
    st = PGStorage()
    try:
        rows = st.list_instruments(active_only=True)
    finally:
        st.close()
    if asset_types:
        allowed = set(asset_types)
        rows = [r for r in rows if r.get("type") in allowed]
    seen = set()
    secids = []
    for row in rows:
        secid = row["secid"]
        if secid in seen:
            continue
        seen.add(secid)
        secids.append(secid)
    return secids


def crawl_one(secid, with_events=True, force=False):
    st = PGStorage()
    try:
        if not force and st.state_is_ok("history", secid):
            print(f"[history_pg] {secid}: 历史已完成，跳过")
            return secid
        kline.crawl(secid, storage=st)
        fundflow.crawl(secid, storage=st)
        fundamental.crawl(secid, storage=st)
        if with_events:
            for cat in EVENT_CATS:
                events.crawl(cat, secid, storage=st)
        st.set_state("history", secid, "done", "ok", None)
    except Exception as e:
        st.set_state("history", secid, None, "failed", str(e))
        print(f"[history_pg] {secid} 失败: {e}")
    finally:
        st.close()
    return secid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secid", action="append", help="Can be specified multiple times.")
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--asset-type", action="append", choices=["stock", "index", "industry", "concept", "board"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--macro", action="store_true")
    ap.add_argument("--no-events", action="store_true")
    ap.add_argument("--workers", type=int, default=systemConfig.MAX_WORKERS)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.macro:
        st = PGStorage()
        try:
            for ind in apiConfig.MACRO_REPORTS:
                try:
                    macro.crawl(ind, storage=st)
                    st.set_state("macro", ind, "done", "ok", None)
                except Exception as e:
                    st.set_state("macro", ind, None, "failed", str(e))
                    print(f"[macro_pg] {ind} 失败: {e}")
        finally:
            st.close()

    secids = list(args.secid or [])
    if args.universe:
        secids = load_pg_universe(args.asset_type)
    if args.limit:
        secids = secids[:args.limit]

    if not secids:
        return

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(crawl_one, s, not args.no_events, args.force) for s in secids]
        for _ in tqdm.tqdm(as_completed(futs), total=len(futs), desc="history_pg"):
            pass


if __name__ == "__main__":
    main()
