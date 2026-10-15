# -*- coding: utf-8 -*-
"""历史资金流（push2his /api/qt/stock/fflow/daykline/get）。

klines[] = 日期,主力净额,小单净额,中单净额,大单净额,超大单净额,...
落地 parquet/fundflow/{secid}/day.parquet。
"""
import sys
import pandas as pd

from config import apiConfig, systemConfig
from eastmoney.client import client
from eastmoney.storage import Storage


def fetch_fundflow(secid, lmt=0):
    data = client.get_qt("/api/qt/stock/fflow/daykline/get", {
        "secid": secid, "ut": systemConfig.UT,
        "fields1": apiConfig.FFLOW_F1, "fields2": apiConfig.FFLOW_F2,
        "lmt": lmt, "klt": 101,
    }, required_data_key="klines")
    if not data or not data.get("data") or not data["data"].get("klines"):
        return pd.DataFrame(columns=apiConfig.FFLOW_COLS)
    rows = []
    for line in data["data"]["klines"]:
        parts = line.split(",")
        rows.append(parts[:len(apiConfig.FFLOW_COLS)])
    df = pd.DataFrame(rows, columns=apiConfig.FFLOW_COLS)
    num = [c for c in apiConfig.FFLOW_COLS if c != "date"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    df.insert(0, "secid", secid)
    return df


def crawl(secid, storage=None, lmt=0):
    own = storage is None
    storage = storage or Storage()
    df = fetch_fundflow(secid, lmt=lmt)
    n = storage.write_parquet("fundflow", secid, "day", df,
                              dedupe_keys=["date"])
    if not df.empty:
        storage.set_cursor("fundflow", secid, df["date"].iloc[-1])
    print(f"[fundflow] {secid}: {len(df)} 行 -> {n}")
    if own:
        storage.close()
    return n


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "1.600909"
    crawl(sid)
