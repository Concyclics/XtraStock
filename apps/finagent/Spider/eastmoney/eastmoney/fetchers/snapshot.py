# -*- coding: utf-8 -*-
"""实时快照（push2 /api/qt/stock/get；本环境走 push2delay 延迟）。

价格类字段按 f59 小数位 /10^decimal 还原（实测 f43:628 -> 6.28）。
落 SQLite valuation_snapshot（估值时序，回测可用作每日因子）。
"""
import sys
import datetime as dt

from config import apiConfig, systemConfig
from eastmoney.client import client
from eastmoney.storage import Storage


def fetch_snapshot(secid):
    data = client.get_qt("/api/qt/stock/get", {
        "secid": secid, "ut": systemConfig.UT,
        "fields": apiConfig.SNAPSHOT_FIELDS,
    })
    if not data or not data.get("data"):
        return None
    raw = data["data"]
    dec = raw.get("f59", 2) or 2
    out = {}
    for f, name in apiConfig.SNAPSHOT_MAP.items():
        v = raw.get(f)
        if v is None or v == "-":
            continue
        if f in apiConfig.SNAPSHOT_SCALED and isinstance(v, (int, float)):
            v = round(v / (10 ** dec), dec)
        out[name] = v
    out["secid"] = secid
    return out


def crawl(secid, storage=None):
    own = storage is None
    storage = storage or Storage()
    s = fetch_snapshot(secid)
    if not s:
        print(f"[snapshot] {secid}: 无数据")
        if own:
            storage.close()
        return 0
    rec = {
        "secid": secid,
        "dt": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pe_ttm": s.get("pe_ttm"), "pb": s.get("pb"),
        "total_mv": s.get("total_mv"), "float_mv": s.get("float_mv"),
        "turnover": s.get("turnover"), "price": s.get("price"),
    }
    n = storage.upsert("valuation_snapshot", [rec])
    print(f"[snapshot] {secid}: price={s.get('price')} "
          f"pe={s.get('pe_ttm')} pb={s.get('pb')} -> {n}")
    if own:
        storage.close()
    return n


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "1.600909"
    crawl(sid)
