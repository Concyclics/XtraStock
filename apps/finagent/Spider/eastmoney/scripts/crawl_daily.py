# -*- coding: utf-8 -*-
"""每日增量：当日 K线 / 快照 / 分时 / 逐笔 + 当日事件。

逐笔与分时无法回填，须每个交易日收盘后运行本脚本累积。
用法:
  python scripts/crawl_daily.py --secid 1.600909
  python scripts/crawl_daily.py --universe
  python scripts/crawl_daily.py --secid 1.600909 --no-ticks
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tqdm
from config import systemConfig
from eastmoney.secid import load_universe
from eastmoney.storage import Storage
from eastmoney.fetchers import (kline, snapshot, trends, ticks,
                                fundflow, events)

EVENT_CATS = ["dragon_tiger", "block_trade", "margin_trade", "northbound"]


def crawl_one(secid, with_ticks=True):
    st = Storage()
    try:
        kline.crawl(secid, tasks=[("day", "qfq"), ("5m", "qfq")],
                    storage=st)
        snapshot.crawl(secid, storage=st)
        trends.crawl(secid, ndays=1, storage=st)
        fundflow.crawl(secid, storage=st)
        if with_ticks:
            ticks.crawl(secid, storage=st)
        for cat in EVENT_CATS:
            events.crawl(cat, secid, storage=st)
    except Exception as e:
        print(f"[daily] {secid} 失败: {e}")
    finally:
        st.close()
    return secid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secid", action="append")
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--no-ticks", action="store_true")
    args = ap.parse_args()

    secids = load_universe() if args.universe else (
        args.secid or load_universe())

    with ThreadPoolExecutor(max_workers=systemConfig.MAX_WORKERS) as ex:
        futs = [ex.submit(crawl_one, s, not args.no_ticks)
                for s in secids]
        for _ in tqdm.tqdm(as_completed(futs), total=len(futs),
                           desc="daily"):
            pass


if __name__ == "__main__":
    main()
