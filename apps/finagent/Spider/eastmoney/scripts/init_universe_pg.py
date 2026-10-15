# -*- coding: utf-8 -*-
"""Initialize Eastmoney universe directly into Postgres.

Usage:
  python scripts/init_universe_pg.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import universeConfig
from eastmoney.fetchers import boards
from eastmoney.pg_storage import PGStorage


GROUPS = [
    ("index", [universeConfig.INDEX_LIST_FS]),
    ("stock", universeConfig.ALL_A_FS_GROUPS),
    ("industry", [universeConfig.INDUSTRY_FS]),
    ("concept", [universeConfig.CONCEPT_FS]),
]


def main():
    st = PGStorage()
    try:
        total = 0
        for typ, filters in GROUPS:
            rows = []
            seen = set()
            for fs in filters:
                for r in boards.fetch_clist(fs):
                    key = (r.get("market"), r.get("code"))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(r)
            recs = []
            for r in rows:
                market_value = r.get("market")
                code_value = r.get("code")
                market = "" if market_value is None else str(market_value)
                code = "" if code_value is None else str(code_value)
                if not market or not code:
                    continue
                r = dict(r)
                r["type"] = typ
                r["secid"] = f"{market}.{code}"
                recs.append(r)
            n = st.upsert("instruments", recs)
            inactive = st.sync_active_universe(typ, [r["secid"] for r in recs])
            total += n
            print(f"[init_universe_pg] {typ}: fetched={len(rows)} upsert={n} inactive={inactive}")
        print(f"[init_universe_pg] total={total}")
    finally:
        st.close()


if __name__ == "__main__":
    main()
