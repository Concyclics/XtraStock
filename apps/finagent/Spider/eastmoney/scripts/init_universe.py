# -*- coding: utf-8 -*-
"""拉取标的池（指数 + 全A + 行业 + 概念）写入 SQLite instruments。

用法: python scripts/init_universe.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eastmoney.fetchers import boards
from eastmoney.storage import Storage


def main():
    st = Storage()
    total = boards.crawl_universe(st)
    n = len(st.list_instruments())
    print(f"[init_universe] 完成，写入 {total} 项，instruments 现有 {n} 条")
    st.close()


if __name__ == "__main__":
    main()
