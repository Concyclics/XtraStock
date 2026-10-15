#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill missing A-share daily bars from Sina into market.bars_daily."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests
import tqdm

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Spider/eastmoney"))

from eastmoney.pg_storage import PGStorage  # noqa: E402
from eastmoney.secid import split_secid  # noqa: E402

SOURCE = "sina:kline:day:none"
SINA_URL = "https://quotes.sina.cn/cn/api/jsonp.php/var%20K=/CN_MarketDataService.getKLineData"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn/",
}


def secid_to_sina(secid: str) -> str | None:
    market, code = split_secid(secid)
    if market == "1":
        return f"sh{code}"
    if market == "0":
        if code.startswith(("8", "4", "920")):
            return f"bj{code}"
        return f"sz{code}"
    return None


def load_secids(path: str) -> list[str]:
    p = Path(path)
    seen: set[str] = set()
    secids: list[str] = []
    if p.suffix.lower() == ".csv":
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "secid" not in reader.fieldnames:
                raise ValueError(f"{path} must contain a secid column")
            for row in reader:
                secid = (row.get("secid") or "").strip()
                if secid and secid not in seen:
                    seen.add(secid)
                    secids.append(secid)
    else:
        for line in p.read_text(encoding="utf-8").splitlines():
            secid = line.strip()
            if secid and not secid.startswith("#") and secid not in seen:
                seen.add(secid)
                secids.append(secid)
    return secids


def load_universe(asset_types: list[str]) -> list[str]:
    st = PGStorage()
    try:
        rows = st.list_instruments(active_only=True)
        # Official filing placeholders may have no Eastmoney price/market-cap
        # snapshot. Keep symbols with recently fetched Sina bars in subsequent
        # daily crawls without fabricating those missing snapshot fields.
        with st.conn.cursor() as cur:
            cur.execute(
                """
                SELECT metadata->>'eastmoney_secid',asset_type
                FROM market.assets
                WHERE metadata ? 'eastmoney_secid'
                  AND metadata->>'sina_latest_bar_date' >= (current_date-45)::text
                  AND coalesce((metadata->>'is_active')::boolean,true)
                  AND coalesce(name,'') !~ '(退市|退$|定转$)'
                """
            )
            rows.extend({"secid": row[0], "type": row[1]} for row in cur.fetchall())
    finally:
        st.close()
    allowed = set(asset_types)
    seen: set[str] = set()
    secids: list[str] = []
    for row in rows:
        secid = row.get("secid")
        if row.get("type") not in allowed or not secid or secid in seen:
            continue
        seen.add(secid)
        secids.append(secid)
    return secids


def fetch_sina_daily(symbol: str, datalen: int, retries: int) -> list[dict[str, Any]]:
    params = {"symbol": symbol, "scale": 240, "ma": "no", "datalen": datalen}
    last_error: Exception | None = None
    for attempt in range(max(1, retries + 1)):
        try:
            r = requests.get(SINA_URL, params=params, headers=HEADERS, timeout=15)
            r.raise_for_status()
            text = r.text
            match = re.search(r"var\s+K=\((.*)\);?\s*$", text, re.S)
            if not match:
                raise ValueError(f"unexpected Sina response: {text[:120]}")
            return json.loads(match.group(1))
        except Exception as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(str(last_error))


def to_decimal(v: Any) -> Decimal | None:
    if v in (None, "", "-"):
        return None
    return Decimal(str(v))


def upsert_rows(
    secid: str,
    rows: list[dict[str, Any]],
    start: str,
    end: str,
    source: str,
    success_state_job: str | None = None,
) -> int:
    selected = [r for r in rows if start <= str(r.get("day", "")) <= end]
    if not selected:
        return 0
    st = PGStorage()
    try:
        asset_id = st.ensure_asset(secid)
        values = [
            (
                asset_id,
                r["day"],
                to_decimal(r.get("open")),
                to_decimal(r.get("high")),
                to_decimal(r.get("low")),
                to_decimal(r.get("close")),
                to_decimal(r.get("volume")),
                None,
                source,
            )
            for r in selected
        ]
        with st.conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO market.bars_daily
                  (asset_id, ts, open, high, low, close, volume, amount, source)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id, ts, source) DO UPDATE
                SET open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    amount = EXCLUDED.amount,
                    updated_at = now()
                """,
                values,
            )
            cur.execute(
                """
                UPDATE market.assets
                SET metadata=metadata || jsonb_build_object(
                    'sina_latest_bar_date',greatest(metadata->>'sina_latest_bar_date',%s::text)
                ),updated_at=now()
                WHERE asset_id=%s
                """,
                (max(str(row["day"]) for row in selected),asset_id),
            )
        st.set_state("kline_backfill:sina:day", secid, end, "ok", None, source="sina")
        if success_state_job:
            st.set_state(success_state_job, secid, end, "ok", None, source="sina")
        return len(values)
    finally:
        st.close()


def crawl_one(
    secid: str,
    start: str,
    end: str,
    datalen: int,
    retries: int,
    source: str,
    success_state_job: str | None = None,
) -> tuple[str, int, str | None]:
    symbol = secid_to_sina(secid)
    if not symbol:
        return secid, 0, "unsupported secid market"
    try:
        rows = fetch_sina_daily(symbol, datalen, retries)
        written = upsert_rows(secid, rows, start, end, source, success_state_job)
        if written <= 0:
            return secid, 0, "no rows in selected date range"
        return secid, written, None
    except Exception as exc:
        st = None
        try:
            st = PGStorage()
            st.set_state(
                "kline_backfill:sina:day", secid, end, "failed", str(exc), source="sina"
            )
        except Exception as state_exc:
            print(f"[sina_kline_backfill] {secid} state update failed: {state_exc}", flush=True)
        finally:
            if st is not None:
                try:
                    st.close()
                except Exception:
                    pass
        return secid, 0, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill missing A-share daily bars from Sina.")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD.")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD.")
    parser.add_argument("--secid", action="append")
    parser.add_argument("--secid-file", help="Text file with one secid per line, or CSV containing a secid column.")
    parser.add_argument("--asset-type", action="append", choices=["stock", "index", "industry", "concept", "board"])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--datalen", type=int, default=80)
    parser.add_argument("--source", default=SOURCE)
    parser.add_argument("--success-state-job", help="Also mark this crawl_state job successful for written symbols.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args()

    secids = list(args.secid or [])
    if args.secid_file:
        secids.extend(load_secids(args.secid_file))
    if not secids:
        secids = load_universe(args.asset_type or ["stock"])
    secids = list(dict.fromkeys(secids))
    if args.limit:
        secids = secids[: args.limit]
    if not secids:
        print("market_kline_sina_backfill: no secids selected")
        return 0

    print(
        f"market_kline_sina_backfill: secids={len(secids)} start={args.start} end={args.end} "
        f"workers={args.workers} source={args.source}",
        flush=True,
    )
    rows_total = 0
    errors = 0
    failed_items=[];empty_items=[];completed=0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                crawl_one, secid, args.start, args.end, args.datalen, args.retries,
                args.source, args.success_state_job,
            )
            for secid in secids
        ]
        for future in tqdm.tqdm(as_completed(futures), total=len(futures), desc="sina_kline_backfill"):
            secid, rows, error = future.result()
            rows_total += rows
            completed+=1
            if not rows and not error: empty_items.append(secid)
            if error:
                errors += 1
                failed_items.append({"secid":secid,"error":str(error)})
                print(f"[sina_kline_backfill] {secid} failed: {error}", flush=True)
    print(f"market_kline_sina_backfill: complete rows={rows_total}, errors={errors}", flush=True)
    summary={"source":args.source,"requested":len(secids),"completed":completed,"rows":rows_total,
        "errors":errors,"failed":failed_items,"empty":empty_items,"start":args.start,"end":args.end,
        "coverage_claim":"Daily bars only; snapshot, minute bars and fund flow are not supplied by this fallback"}
    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True,exist_ok=True)
        args.summary_output.write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    return 1 if errors and rows_total==0 else (2 if errors else 0)


if __name__ == "__main__":
    raise SystemExit(main())
