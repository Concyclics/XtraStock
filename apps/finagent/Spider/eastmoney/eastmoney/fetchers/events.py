# -*- coding: utf-8 -*-
"""事件/另类数据（datacenter-web /api/data/v1/get，reportName 表驱动）。

龙虎榜/大宗交易/融资融券/限售解禁/机构调研/北向资金。各报表列结构
差异大，v1 统一整行 JSON 落 SQLite events(category, secid, trade_date,
payload)，回测侧再按需解析。支持按个股代码过滤或全市场最近 N 条。
"""
import json
import sys

from config import apiConfig
from eastmoney.client import client
from eastmoney.secid import split_secid
from eastmoney.storage import Storage

# 各报表的代码过滤列与日期列（按东财 datacenter 约定）
_CODE_COL = {
    "dragon_tiger": "SECURITY_CODE", "block_trade": "SECURITY_CODE",
    "margin_trade": "SCODE", "unlock": "SECURITY_CODE",
    "survey": "SECURITY_CODE", "northbound": "SECURITY_CODE",
}
_DATE_COL = {
    "dragon_tiger": "TRADE_DATE", "block_trade": "TRADE_DATE",
    "margin_trade": "DATE", "unlock": "FREE_DATE",
    "survey": "RECEIVE_START_DATE", "northbound": "TRADE_DATE",
}


def fetch_event(category, code=None, max_pages=20, page_size=200):
    report = apiConfig.EVENT_REPORTS[category]
    date_col = _DATE_COL.get(category, "TRADE_DATE")
    params = {
        "reportName": report, "columns": "ALL",
        "pageSize": page_size, "sortColumns": date_col, "sortTypes": -1,
    }
    if code:
        params["filter"] = f'({_CODE_COL[category]}="{code}")'
    rows, page = [], 1
    while page <= max_pages:
        params["pageNumber"] = page
        data = client.datacenter(params)
        if not data or not data.get("result"):
            break
        chunk = data["result"].get("data") or []
        rows.extend(chunk)
        if page >= data["result"].get("pages", 1) or not chunk:
            break
        page += 1
    return rows, date_col


def crawl(category, secid=None, storage=None):
    own = storage is None
    storage = storage or Storage()
    code = split_secid(secid)[1] if secid else None
    rows, date_col = fetch_event(category, code)
    recs = []
    for r in rows:
        recs.append({
            "category": category,
            "secid": secid or "",
            "trade_date": str(r.get(date_col, ""))[:10],
            "payload": json.dumps(r, ensure_ascii=False),
        })
    n = storage.upsert("events", recs)
    print(f"[events] {category} secid={secid}: {len(rows)} 行 -> {n}")
    if own:
        storage.close()
    return n


if __name__ == "__main__":
    cat = sys.argv[1] if len(sys.argv) > 1 else "dragon_tiger"
    sid = sys.argv[2] if len(sys.argv) > 2 else None
    crawl(cat, sid)
