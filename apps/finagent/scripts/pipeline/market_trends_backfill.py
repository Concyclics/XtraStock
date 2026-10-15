#!/usr/bin/env python3
"""Backfill a single recent trading day's Eastmoney one-minute trends."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import tqdm

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(ROOT, "Spider/eastmoney"))

from eastmoney.fetchers import trends  # noqa: E402
from eastmoney.pg_storage import PGStorage  # noqa: E402


def load_secids(asset_types: list[str]) -> list[str]:
    storage = PGStorage()
    try:
        rows = storage.list_instruments(active_only=True)
    finally:
        storage.close()
    allowed = set(asset_types)
    return sorted({row["secid"] for row in rows if row["type"] in allowed})


def backfill_one(secid: str, target_date: str, ndays: int, force: bool, state_job: str) -> tuple[str, int, str | None]:
    storage = None
    try:
        # PostgreSQL may temporarily reject connections while crash recovery
        # reaches consistency. Retrying here preserves the rest of the batch.
        last_error = None
        for attempt in range(6):
            try:
                storage = PGStorage()
                break
            except Exception as exc:
                last_error = exc
                time.sleep(min(15, 1.5 * (attempt + 1)))
        if storage is None:
            return "failed", 0, f"database connection unavailable: {last_error}"
        if not force and storage.state_is_ok(state_job, secid, target_date):
            return "skipped", 0, None
        frame = trends.fetch_trends(secid, ndays=ndays)
        if frame.empty:
            storage.set_state(state_job, secid, target_date, "failed", "empty trends response")
            return "failed", 0, "empty trends response"
        selected = frame.loc[frame["time"].astype(str).str[:10] == target_date].copy()
        if selected.empty:
            storage.set_state(state_job, secid, target_date, "failed", "target date absent from trends response")
            return "failed", 0, "target date absent from trends response"
        written = storage.upsert_trends(secid, selected)
        storage.set_state(state_job, secid, target_date, "ok", None)
        return "ok", written, None
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        try:
            storage.set_state(state_job, secid, target_date, "failed", message)
        except Exception:
            pass
        return "failed", 0, message
    finally:
        if storage is not None:
            storage.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill Eastmoney one-minute trends for one recent date.")
    parser.add_argument("--date", required=True, help="Trading date, YYYY-MM-DD.")
    parser.add_argument("--ndays", type=int, default=1, help="Recent window requested from Eastmoney.")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--asset-type", action="append", choices=["stock", "index"], default=["stock"])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--state-job", default="trends_backfill_1m")
    args = parser.parse_args()

    target_date = dt.date.fromisoformat(args.date).isoformat()
    secids = load_secids(args.asset_type)
    if args.limit:
        secids = secids[:args.limit]
    if not secids:
        print("market_trends_backfill: no matching instruments")
        return 0

    print(
        f"market_trends_backfill: secids={len(secids)} date={target_date} "
        f"ndays={args.ndays} workers={args.workers}",
        flush=True,
    )
    counts = {"ok": 0, "skipped": 0, "failed": 0, "rows": 0}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(backfill_one, secid, target_date, args.ndays, args.force, args.state_job): secid
            for secid in secids
        }
        for future in tqdm.tqdm(as_completed(futures), total=len(futures), desc="trends_backfill"):
            secid = futures[future]
            try:
                status, rows, error = future.result()
            except Exception as exc:
                status, rows, error = "failed", 0, f"worker crash: {exc}"
            counts[status] += 1
            counts["rows"] += rows
            if error:
                print(f"[trends_backfill] {secid} failed: {error}", flush=True)
    print(
        "market_trends_backfill: complete "
        f"ok={counts['ok']} skipped={counts['skipped']} failed={counts['failed']} rows={counts['rows']}",
        flush=True,
    )
    return 0 if counts["failed"] <= max(100, len(secids) // 2) else 1


if __name__ == "__main__":
    raise SystemExit(main())
