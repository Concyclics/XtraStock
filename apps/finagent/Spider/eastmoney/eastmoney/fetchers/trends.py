# -*- coding: utf-8 -*-
"""分时分钟数据（push2 /api/qt/stock/trends2/get；本环境走 push2delay）。

trends[] = 时间,开,收,高,低,成交量,成交额,均价。ndays=1 当日，
ndays=5 近5日。注意：分时只能从采集日起向前累积，无法回填历史。
落 parquet/trends/{secid}/{date}.parquet。
"""
import sys
import pandas as pd

from config import apiConfig, systemConfig
from eastmoney.client import client
from eastmoney.storage import Storage


def fetch_trends(secid, ndays=1):
    data = client.get_qt("/api/qt/stock/trends2/get", {
        "secid": secid, "ut": systemConfig.UT,
        "fields1": apiConfig.TRENDS_F1, "fields2": apiConfig.TRENDS_F2,
        "ndays": ndays, "iscr": 0, "forcect": 1,
    }, required_data_key="trends")
    if not data or not data.get("data") or not data["data"].get("trends"):
        return pd.DataFrame(columns=apiConfig.TRENDS_COLS)
    rows = [t.split(",") for t in data["data"]["trends"]]
    df = pd.DataFrame(rows, columns=apiConfig.TRENDS_COLS)
    num = [c for c in apiConfig.TRENDS_COLS if c != "time"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    df.insert(0, "secid", secid)
    return df


def crawl(secid, ndays=1, storage=None):
    own = storage is None
    storage = storage or Storage()
    df = fetch_trends(secid, ndays)
    total = 0
    for date, g in df.groupby(df["time"].str[:10]):
        total += storage.write_parquet("trends", secid, date, g,
                                       dedupe_keys=["time"])
    if not df.empty:
        storage.set_cursor("trends", secid, df["time"].iloc[-1])
    print(f"[trends] {secid} ndays={ndays}: {len(df)} 行 -> {total}")
    if own:
        storage.close()
    return total


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "1.600909"
    crawl(sid, int(sys.argv[2]) if len(sys.argv) > 2 else 1)
