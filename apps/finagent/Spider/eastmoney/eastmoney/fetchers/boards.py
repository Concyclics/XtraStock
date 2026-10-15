# -*- coding: utf-8 -*-
"""标的列表 / 板块（push2 /api/qt/clist/get；本环境走 push2delay）。

fs 过滤串切换：沪深指数 b:MK0010、全A股、行业 m:90+t:2、概念 m:90+t:3。
data.diff 为 {序号: {fxx:...}} 字典，分页 pn/pz。写入 SQLite instruments。
"""
import sys

from config import apiConfig, systemConfig, universeConfig
from eastmoney.client import client
from eastmoney.storage import Storage


def fetch_clist(fs, fields=None, page_size=200):
    fields = fields or apiConfig.CLIST_FIELDS
    out, pn = [], 1
    while True:
        data = client.get_qt("/api/qt/clist/get", {
            "ut": systemConfig.UT, "pn": pn, "pz": page_size,
            "po": 1, "fid": "f3", "fs": fs, "fields": fields,
        }, required_data_key="diff")
        if not data or not data.get("data"):
            break
        diff = data["data"].get("diff")
        total = data["data"].get("total", 0)
        if not diff:
            break
        items = diff.values() if isinstance(diff, dict) else diff
        for it in items:
            rec = {apiConfig.CLIST_MAP.get(k, k): v for k, v in it.items()}
            out.append(rec)
        if pn * page_size >= total:
            break
        pn += 1
    return out


def crawl_universe(storage=None):
    """拉指数 + 全A + 行业 + 概念，写入 instruments。"""
    own = storage is None
    storage = storage or Storage()
    groups = [
        ("index", [universeConfig.INDEX_LIST_FS]),
        ("stock", universeConfig.ALL_A_FS_GROUPS),
        ("industry", [universeConfig.INDUSTRY_FS]),
        ("concept", [universeConfig.CONCEPT_FS]),
    ]
    total = 0
    for typ, filters in groups:
        rows, seen = [], set()
        for fs in filters:
            for r in fetch_clist(fs):
                key = (r.get("market"), r.get("code"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)
        recs = []
        for r in rows:
            mkt = str(r.get("market", ""))
            code = str(r.get("code", ""))
            if not code:
                continue
            recs.append({
                "secid": f"{mkt}.{code}", "code": code,
                "name": r.get("name", ""), "market": mkt,
                "type": typ, "board": "",
            })
        n = storage.upsert("instruments", recs)
        total += n
        print(f"[boards] {typ}: {len(rows)} 项 -> {n}")
    if own:
        storage.close()
    return total


if __name__ == "__main__":
    if len(sys.argv) > 1:
        for x in fetch_clist(sys.argv[1])[:10]:
            print(x)
    else:
        crawl_universe()
