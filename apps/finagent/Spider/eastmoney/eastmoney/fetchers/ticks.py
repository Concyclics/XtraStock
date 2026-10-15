# -*- coding: utf-8 -*-
"""逐笔成交明细（push2 /api/qt/stock/details/get；本环境走 push2delay）。

details[] = 时间,价,手数,方向(1买/2卖/4集合竞价)。pos 负偏移取最近 N
笔，单次上限约 4000，需翻页拉全当日。注意：逐笔仅当日，无法回填历史，
须每个交易日收盘后采集。落 parquet/ticks/{secid}/{date}.parquet。
"""
import sys
import datetime as dt
import pandas as pd

from config import apiConfig, systemConfig
from eastmoney.client import client
from eastmoney.storage import Storage


def fetch_ticks(secid, want=20000):
    """从最新往回翻页拉取当日逐笔，直到取满或无更多。"""
    page = apiConfig.DETAILS_PAGE
    collected, pos = [], page
    while len(collected) < want:
        data = client.get_qt("/api/qt/stock/details/get", {
            "secid": secid, "ut": systemConfig.UT_DETAILS,
            "fields1": apiConfig.DETAILS_F1,
            "fields2": apiConfig.DETAILS_F2,
            "pos": f"-{pos}",
        }, required_data_key="details")
        if not data or not data.get("data"):
            break
        chunk = data["data"].get("details") or []
        if not chunk:
            break
        collected = chunk + collected if pos > page else chunk
        if len(chunk) < page:
            break
        pos += page
    rows = []
    for line in collected:
        p = line.split(",")
        if len(p) >= 4:
            rows.append(p[:4])
    df = pd.DataFrame(rows, columns=apiConfig.DETAILS_COLS)
    if not df.empty:
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df["direction"] = pd.to_numeric(df["direction"], errors="coerce")
        df.insert(0, "secid", secid)
    return df


def crawl(secid, storage=None):
    own = storage is None
    storage = storage or Storage()
    df = fetch_ticks(secid)
    date = dt.date.today().strftime("%Y-%m-%d")
    n = storage.write_parquet("ticks", secid, date, df,
                              dedupe_keys=["time", "price",
                                           "volume", "direction"])
    if not df.empty:
        storage.set_cursor("ticks", secid, f"{date}:{df['time'].iloc[-1]}")
    print(f"[ticks] {secid} {date}: {len(df)} 笔 -> {n}")
    if own:
        storage.close()
    return n


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "1.600909"
    crawl(sid)
