# -*- coding: utf-8 -*-
"""Order book snapshot via Eastmoney quote API.

The public field layout has changed over time, so v1 stores the full raw
payload and only materializes stable imbalance fields when present.
"""
import sys

from config import systemConfig
from eastmoney.client import client
from eastmoney.storage import Storage

ORDER_BOOK_FIELDS = (
    "f31,f32,f33,f34,f35,f36,f37,f38,f39,f40,"
    "f19,f20,f17,f18,f15,f16,f13,f14,f11,f12,"
    "f191,f192"
)


def fetch_orderbook(secid):
    data = client.get_qt("/api/qt/stock/get", {
        "secid": secid,
        "ut": systemConfig.UT,
        "fields": ORDER_BOOK_FIELDS,
    })
    if not data or not data.get("data"):
        return None
    out = dict(data["data"])
    out["secid"] = secid
    return out


def crawl(secid, storage=None):
    own = storage is None
    storage = storage or Storage()
    payload = fetch_orderbook(secid)
    if not payload:
        print(f"[orderbook] {secid}: 无数据")
        if own:
            storage.close()
        return 0
    if hasattr(storage, "upsert_order_book"):
        n = storage.upsert_order_book(secid, payload)
    else:
        n = 0
    print(f"[orderbook] {secid}: -> {n}")
    if own:
        storage.close()
    return n


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "1.600909"
    crawl(sid)
