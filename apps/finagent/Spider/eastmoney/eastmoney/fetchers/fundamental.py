# -*- coding: utf-8 -*-
"""基本面 / F10（datacenter-web /api/data/v1/get）。

业绩报表 RPT_LICO_FN_CPD 按 SECURITY_CODE 过滤，整行 JSON 落 SQLite
financials（item 维度展开关键指标，原始行存 events 备查）。
"""
import json
import sys

from eastmoney.client import client
from eastmoney.secid import split_secid
from eastmoney.storage import Storage

# RPT_LICO_FN_CPD 关键列 -> 统一 item 名
_FIN_ITEMS = {
    "TOTAL_OPERATE_INCOME": "营业总收入",
    "PARENT_NETPROFIT": "归母净利润",
    "BASIC_EPS": "每股收益",
    "WEIGHTAVG_ROE": "净资产收益率",
    "XSMLL": "毛利率",
    "BPS": "每股净资产",
    "MGJYXJJE": "每股经营现金流",
    "YSTZ": "营收同比", "SJLTZ": "净利同比",
}


def fetch_financials(code):
    rows, page = [], 1
    while True:
        data = client.datacenter({
            "reportName": "RPT_LICO_FN_CPD", "columns": "ALL",
            "pageNumber": page, "pageSize": 100,
            "sortColumns": "REPORTDATE", "sortTypes": -1,
            "filter": f'(SECURITY_CODE="{code}")',
        })
        if not data or not data.get("result"):
            break
        chunk = data["result"].get("data") or []
        rows.extend(chunk)
        if page >= data["result"].get("pages", 1) or not chunk:
            break
        page += 1
    return rows


def crawl(secid, storage=None):
    own = storage is None
    storage = storage or Storage()
    _, code = split_secid(secid)
    rows = fetch_financials(code)
    fin, raw = [], []
    for r in rows:
        rdate = str(r.get("REPORTDATE", ""))[:10]
        for col, item in _FIN_ITEMS.items():
            v = r.get(col)
            if v is not None:
                fin.append({"secid": secid, "report_date": rdate,
                            "item": item, "value": v})
        raw.append({"category": "financials_raw", "secid": secid,
                    "trade_date": rdate,
                    "payload": json.dumps(r, ensure_ascii=False)})
    n = storage.upsert("financials", fin)
    storage.upsert("events", raw)
    print(f"[fundamental] {secid}: {len(rows)} 期 -> {n} 项")
    if own:
        storage.close()
    return n


if __name__ == "__main__":
    sid = sys.argv[1] if len(sys.argv) > 1 else "1.600909"
    crawl(sid)
