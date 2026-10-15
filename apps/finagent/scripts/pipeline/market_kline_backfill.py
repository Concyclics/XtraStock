#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill Eastmoney daily K-line bars into Postgres."""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import tqdm

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Spider/eastmoney"))

from eastmoney.fetchers import kline  # noqa: E402
from eastmoney.pg_storage import PGStorage  # noqa: E402

KLINE_PROBE_SECIDS = ("0.000001", "1.600519", "0.300750")
KLINE_PROBE_BEG = "20250101"
KLINE_PROBE_END = "20251231"


def load_universe(asset_types: list[str]) -> list[str]:
    storage = PGStorage()
    try:
        rows = storage.list_instruments(active_only=True)
    finally:
        storage.close()
    allowed = set(asset_types)
    secids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        secid = row.get("secid")
        if row.get("type") not in allowed or not secid or secid in seen:
            continue
        seen.add(secid)
        secids.append(secid)
    return secids


def load_secid_file(path: str) -> list[str]:
    file_path = Path(path)
    secids: list[str] = []
    seen: set[str] = set()
    if file_path.suffix.lower() == ".csv":
        with file_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or "secid" not in reader.fieldnames:
                raise ValueError(f"{path} must contain a secid column")
            for row in reader:
                secid = (row.get("secid") or "").strip()
                if secid and secid not in seen:
                    seen.add(secid)
                    secids.append(secid)
    else:
        for line in file_path.read_text(encoding="utf-8").splitlines():
            secid = line.strip()
            if not secid or secid.startswith("#") or secid in seen:
                continue
            seen.add(secid)
            secids.append(secid)
    return secids


def probe_kline_api(adjusts: list[str]) -> None:
    for adjust in adjusts:
        available = False
        for secid in KLINE_PROBE_SECIDS:
            df = kline.fetch_kline(
                secid, period="day", adjust=adjust,
                beg=KLINE_PROBE_BEG, end=KLINE_PROBE_END,
            )
            if not df.empty:
                print(
                    f"market_kline_backfill: kline probe ok secid={secid} "
                    f"adjust={adjust} rows={len(df)}", flush=True,
                )
                available = True
                break
        if not available:
            raise RuntimeError(
                f"Eastmoney kline probe failed adjust={adjust}; "
                "aborting to avoid writing mass failed states"
            )


def filter_unfinished(
    secids: list[str], end: str, state_job: str, skip_failed: bool = False,
) -> list[str]:
    storage = PGStorage()
    try:
        unfinished = []
        for secid in secids:
            state = storage.get_state(state_job, secid)
            if state and state.get("status") == "ok" and state.get("last_cursor") == end:
                continue
            if skip_failed and state and state.get("status") == "failed" and state.get("last_cursor") == end:
                continue
            unfinished.append(secid)
        return unfinished
    finally:
        storage.close()


def crawl_one(
    secid: str, beg: str, end: str, adjusts: list[str], retries: int, state_job: str,
) -> tuple[str, int, str | None]:
    storage = PGStorage()
    total = 0
    try:
        failed_adjusts = []
        for adjust in adjusts:
            rows = 0
            for attempt in range(max(1, retries + 1)):
                rows = kline.crawl(
                    secid,
                    tasks=[("day", adjust)],
                    storage=storage,
                    beg=beg,
                    end=end,
                )
                if rows > 0:
                    break
            total += rows
            if rows <= 0:
                failed_adjusts.append(adjust)
        if failed_adjusts:
            error = "empty kline response: " + ",".join(failed_adjusts)
            storage.set_state(state_job, secid, end, "failed", error)
            return secid, total, error
        else:
            storage.set_state(state_job, secid, end, "ok", None)
        return secid, total, None
    except Exception as exc:
        try:
            storage.set_state(state_job, secid, end, "failed", str(exc))
        except Exception:
            pass
        return secid, total, str(exc)
    finally:
        storage.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill daily Eastmoney K-line bars into market.bars_daily.")
    parser.add_argument("--beg", default="19900101", help="Start date YYYYMMDD. Default fetches from listing history.")
    parser.add_argument("--end", default="20500101", help="End date YYYYMMDD.")
    parser.add_argument("--adjust", action="append", choices=["qfq", "none", "hfq"], default=None)
    parser.add_argument("--asset-type", action="append", choices=["stock", "index", "industry", "concept", "board"])
    parser.add_argument("--secid", action="append", help="Optional secid such as 0.002594. If omitted, uses database universe.")
    parser.add_argument("--secid-file", help="Text file with one secid per line, or CSV containing a secid column.")
    parser.add_argument("--workers", type=int, default=int(os.getenv("MARKET_BACKFILL_WORKERS", "8")))
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--skip-ok", action="store_true", help="Skip secids already marked ok for this end cursor.")
    parser.add_argument("--skip-failed", action="store_true", help="Skip secids already marked failed for this end cursor.")
    parser.add_argument("--no-kline-probe", action="store_true", help="Disable startup probe for Eastmoney kline API health.")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    adjusts = args.adjust or ["qfq"]
    state_job = "kline_backfill:day" if adjusts == ["qfq"] else "kline_backfill:day:" + ",".join(adjusts)
    asset_types = args.asset_type or ["stock", "index"]
    secids = list(args.secid or [])
    if args.secid_file:
        secids.extend(load_secid_file(args.secid_file))
    if not secids:
        secids = load_universe(asset_types)
    secids = list(dict.fromkeys(secids))
    if args.limit:
        secids = secids[: args.limit]
    if args.skip_ok:
        before = len(secids)
        secids = filter_unfinished(secids, args.end, state_job, args.skip_failed)
        skipped = "skip-ok/failed" if args.skip_failed else "skip-ok"
        print(f"market_kline_backfill: {skipped} filtered {before} -> {len(secids)}", flush=True)
    if not secids:
        print("market_kline_backfill: no secids selected")
        return 0
    if not args.no_kline_probe:
        probe_kline_api(adjusts)

    print(
        f"market_kline_backfill: secids={len(secids)}, beg={args.beg}, end={args.end}, "
        f"adjusts={adjusts}, workers={args.workers}, asset_types={asset_types}",
        flush=True,
    )
    rows_total = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(crawl_one, secid, args.beg, args.end, adjusts, args.retries, state_job)
            for secid in secids
        ]
        for future in tqdm.tqdm(as_completed(futures), total=len(futures), desc="kline_backfill"):
            secid, rows, error = future.result()
            rows_total += rows
            if error:
                errors += 1
                print(f"[kline_backfill] {secid} failed: {error}", flush=True)
    print(f"market_kline_backfill: complete rows={rows_total}, errors={errors}", flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
