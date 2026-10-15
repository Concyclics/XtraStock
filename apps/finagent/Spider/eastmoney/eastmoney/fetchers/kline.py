# -*- coding: utf-8 -*-
"""多周期 K 线（历史核心，push2his /api/qt/stock/kline/get）。

klt 周期 + fqt 复权见 config.ApiConfig；返回逗号串 klines[] 按
KLINE_COLS 解析为 DataFrame，落地 parquet/kline/{secid}/{klt}_{fqt}.parquet。
"""
import sys
import pandas as pd

from config import apiConfig, systemConfig
from eastmoney.client import client
from eastmoney.storage import Storage


def fetch_kline(secid, period="day", adjust="qfq",
                beg="19900101", end="20500101"):
    klt = apiConfig.KLT[period]
    fqt = apiConfig.FQT[adjust]
    data = client.get_qt("/api/qt/stock/kline/get", {
        "secid": secid, "ut": systemConfig.UT,
        "klt": klt, "fqt": fqt,
        "fields1": apiConfig.KLINE_F1, "fields2": apiConfig.KLINE_F2,
        "beg": beg, "end": end,
    }, required_data_key="klines")
    if not data or not data.get("data") or not data["data"].get("klines"):
        return pd.DataFrame(columns=apiConfig.KLINE_COLS)
    rows = [line.split(",") for line in data["data"]["klines"]]
    df = pd.DataFrame(rows, columns=apiConfig.KLINE_COLS)
    num = [c for c in apiConfig.KLINE_COLS if c != "date"]
    df[num] = df[num].apply(pd.to_numeric, errors="coerce")
    df.insert(0, "secid", secid)
    return df


def crawl(secid, tasks=None, storage=None, beg="19900101", end="20500101"):
    """采集多组 (周期, 复权) 并 upsert 落地，记录续采游标。"""
    tasks = tasks or apiConfig.KLINE_TASKS
    own = storage is None
    storage = storage or Storage()
    total = 0
    for period, adjust in tasks:
        df = fetch_kline(secid, period, adjust, beg=beg, end=end)
        n = storage.write_parquet("kline", secid,
                                  f"{apiConfig.KLT[period]}_{adjust}",
                                  df, dedupe_keys=["date"])
        if not df.empty:
            storage.set_cursor(f"kline:{period}:{adjust}", secid,
                               df["date"].iloc[-1])
        total += n
        print(f"[kline] {secid} {period}/{adjust}: {len(df)} 行 -> {n}")
    if own:
        storage.close()
    return total


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "1.600909"
    crawl(sid)
