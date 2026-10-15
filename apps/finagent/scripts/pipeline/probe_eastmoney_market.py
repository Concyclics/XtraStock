#!/usr/bin/env python3
"""Bound the history-endpoint check before a whole-market crawl."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Spider/eastmoney"))

from config import systemConfig
from eastmoney.client import EastMoneyClient


def main() -> int:
    # This short-lived process changes no settings in the real crawler.
    systemConfig.TIMEOUT = 4
    systemConfig.MAX_RETRIES = 1
    result = EastMoneyClient().get_qt(
        "/api/qt/stock/kline/get",
        {
            "secid": "1.600519", "klt": 101, "fqt": 1, "lmt": 5,
            "beg": "0", "end": "20500101",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
        required_data_key="klines",
    )
    available = bool(result and (result.get("data") or {}).get("klines"))
    print(json.dumps({"eastmoney_history_available": available, "bounded_read_only_probe": True}))
    return 0 if available else 1


if __name__ == "__main__":
    raise SystemExit(main())
