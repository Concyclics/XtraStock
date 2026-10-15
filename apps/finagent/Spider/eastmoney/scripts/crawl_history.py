# -*- coding: utf-8 -*-
"""历史回填：K线 / 资金流 / 财务 / 宏观（可全历史回溯的数据）。

并发模式参照 YiLinSpider/main.py：ThreadPoolExecutor + tqdm + 限速。
用法:
  python scripts/crawl_history.py --secid 1.600909
  python scripts/crawl_history.py --universe          # 用 instruments 全表
  python scripts/crawl_history.py --secid 1.600909 --macro
"""
import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tqdm
from config import apiConfig, systemConfig
from eastmoney.secid import load_universe
from eastmoney.storage import Storage
from eastmoney.fetchers import kline, fundflow, fundamental, macro


def crawl_one(secid):
    st = Storage()
    try:
        kline.crawl(secid, storage=st)
        fundflow.crawl(secid, storage=st)
        fundamental.crawl(secid, storage=st)
    except Exception as e:
        print(f"[history] {secid} 失败: {e}")
    finally:
        st.close()
    return secid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secid", action="append", help="可多次指定")
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--macro", action="store_true",
                    help="同时回填全部宏观指标")
    args = ap.parse_args()

    if args.macro:
        st = Storage()
        for ind in apiConfig.MACRO_REPORTS:
            try:
                macro.crawl(ind, storage=st)
            except Exception as e:
                print(f"[macro] {ind} 失败: {e}")
        st.close()

    secids = list(args.secid or [])
    if args.universe:
        secids = load_universe()
    if not secids and not args.macro:
        secids = load_universe()
    if not secids:
        return

    with ThreadPoolExecutor(max_workers=systemConfig.MAX_WORKERS) as ex:
        futs = [ex.submit(crawl_one, s) for s in secids]
        for _ in tqdm.tqdm(as_completed(futs), total=len(futs),
                           desc="history"):
            pass


if __name__ == "__main__":
    main()
