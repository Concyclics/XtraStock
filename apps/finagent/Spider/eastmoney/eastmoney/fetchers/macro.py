# -*- coding: utf-8 -*-
"""宏观经济数据（datacenter-web /api/data/v1/get，reportName 表驱动）。

CPI/PPI/GDP/PMI/M2/社融/LPR/汇率... 仅 reportName 不同，统一分页拉取
全历史，整行 JSON 落 SQLite macro 表（payload 存 JSON 文本）。
"""
import json
import sys

from config import apiConfig
from eastmoney.client import client
from eastmoney.storage import Storage


def fetch_macro(indicator):
    report = apiConfig.MACRO_REPORTS[indicator]
    rows, page = [], 1
    while True:
        data = client.datacenter({
            "reportName": report, "columns": "ALL",
            "pageNumber": page, "pageSize": 500,
            "sortColumns": "REPORT_DATE", "sortTypes": -1,
        })
        if not data or not data.get("result"):
            break
        chunk = data["result"].get("data") or []
        rows.extend(chunk)
        if page >= data["result"].get("pages", 1) or not chunk:
            break
        page += 1
    return rows


def crawl(indicator, storage=None):
    own = storage is None
    storage = storage or Storage()
    rows = fetch_macro(indicator)
    recs = []
    for r in rows:
        period = (r.get("REPORT_DATE") or r.get("TIME")
                  or r.get("REPORT_TIME") or "")
        recs.append({"indicator": indicator, "period": str(period),
                     "payload": json.dumps(r, ensure_ascii=False)})
    n = storage.upsert("macro", recs)
    print(f"[macro] {indicator}: {n} 期")
    if own:
        storage.close()
    return n


if __name__ == "__main__":
    ind = sys.argv[1] if len(sys.argv) > 1 else "CPI"
    crawl(ind)
