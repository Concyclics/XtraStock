# -*- coding: utf-8 -*-
"""Daily Eastmoney incremental crawl directly into Postgres.

Usage:
  python scripts/crawl_daily_pg.py --secid 1.600909
  python scripts/crawl_daily_pg.py --universe --no-ticks
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tqdm

from config import systemConfig
from eastmoney.pg_storage import PGStorage
from eastmoney.client import client
from eastmoney.health import probe_market_feeds
from eastmoney.fetchers import kline, snapshot, trends, ticks, fundflow, events, orderbook, news


EVENT_CATS = ["dragon_tiger", "block_trade", "margin_trade", "northbound"]
TZ = ZoneInfo("Asia/Shanghai")
KLINE_PROBE_SECIDS = ("0.000001", "1.600519", "0.300750")
KLINE_PROBE_BEG = "20250101"
KLINE_PROBE_END = "20251231"


def yyyymmdd(days_back, base_date=None):
    base = base_date or dt.datetime.now(TZ).date()
    return (base - dt.timedelta(days=days_back)).strftime("%Y%m%d")


def today_cursor(cursor_date=None):
    return (cursor_date or dt.datetime.now(TZ).date()).isoformat()


def load_pg_universe(asset_types=None):
    st = PGStorage()
    try:
        rows = st.list_instruments(active_only=True)
    finally:
        st.close()
    if asset_types:
        allowed = set(asset_types)
        rows = [r for r in rows if r.get("type") in allowed]
    seen = set()
    secids = []
    for row in rows:
        secid = row["secid"]
        if secid in seen:
            continue
        seen.add(secid)
        secids.append(secid)
    return secids


def probe_kline_api():
    for secid in KLINE_PROBE_SECIDS:
        qfq = kline.fetch_kline(secid, period="day", adjust="qfq", beg=KLINE_PROBE_BEG, end=KLINE_PROBE_END)
        raw = kline.fetch_kline(secid, period="day", adjust="none", beg=KLINE_PROBE_BEG, end=KLINE_PROBE_END)
        if not qfq.empty and not raw.empty:
            print(f"[daily_pg] kline probe ok: {secid} qfq={len(qfq)} none={len(raw)}")
            return True
    print("[daily_pg] kline probe failed; continuing with independent per-source collection")
    return False


def set_state_resilient(job, target, cursor, status, error, attempts=4, storage=None):
    """Write crawl state, reconnecting only when the worker connection is broken."""
    if storage is not None:
        try:
            storage.set_state(job, target, cursor, status, error)
            return True
        except Exception:
            pass
    last_error = None
    for attempt in range(attempts):
        state_storage = None
        try:
            state_storage = PGStorage()
            state_storage.set_state(job, target, cursor, status, error)
            return True
        except Exception as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(0.5 * (attempt + 1))
        finally:
            if state_storage is not None:
                try:
                    state_storage.close()
                except Exception:
                    pass
    print(f"[daily_pg] {target} state update failed after {attempts} attempts: {last_error}")
    return False


def crawl_one(
    secid,
    with_kline=True,
    with_daily_kline=True,
    with_ticks=True,
    with_orderbook=True,
    with_events=True,
    force=False,
    cursor_date=None,
    state_job="daily",
    feeds=None,
):
    st = PGStorage()
    try:
        cursor = today_cursor(cursor_date)
        feeds = feeds or {"daily": True, "snapshot": True, "trends": True, "fundflow": True}
        if not force and st.state_is_ok(state_job, secid, cursor):
            print(f"[daily_pg] {secid}: 今日已完成，跳过")
            return "ok"

        day_qfq_rows = 0
        day_none_rows = 0
        if with_kline and with_daily_kline:
            try:
                day_qfq_rows = kline.crawl(
                    secid, tasks=[("day", "qfq")], storage=st,
                    beg=yyyymmdd(10, cursor_date),
                )
            except Exception as exc:
                print(f"[daily_pg] {secid} warning: day/qfq kline failed: {exc}")
            try:
                day_none_rows = kline.crawl(
                    secid, tasks=[("day", "none")], storage=st,
                    beg=yyyymmdd(10, cursor_date),
                )
            except Exception as exc:
                print(f"[daily_pg] {secid} warning: day/none kline failed: {exc}")
            if day_qfq_rows <= 0:
                print(f"[daily_pg] {secid} warning: empty day/qfq kline response")
            if day_none_rows <= 0:
                print(f"[daily_pg] {secid} warning: empty day/none kline response")
        elif not with_kline:
            print(f"[daily_pg] {secid} warning: skipping K-line after startup probe failure")
        else:
            print(f"[daily_pg] {secid}: daily K-line disabled; collecting intraday feeds only")

        def collect_snapshot():
            payload = snapshot.fetch_snapshot(secid)
            return st.upsert_quote_snapshot(secid, payload, raw=payload) if payload else 0

        # Each non-daily feed is best-effort. A K-line outage must not lose
        # the narrow retention window of intraday data.
        for label, collect in (
            ("snapshot", collect_snapshot),
            ("trends", lambda: trends.crawl(secid, ndays=1, storage=st)),
            ("fundflow", lambda: fundflow.crawl(secid, storage=st, lmt=10)),
        ):
            if not feeds.get(label):
                continue
            try:
                count = collect()
                set_state_resilient(state_job + ":" + label, secid, cursor,
                                    "ok" if count else "failed",
                                    None if count else "source returned no records", storage=st)
            except Exception as exc:
                print(f"[daily_pg] {secid} warning: {label} failed: {exc}")
        if with_kline:
            try:
                kline.crawl(secid, tasks=[("5m", "qfq")], storage=st, beg=yyyymmdd(7, cursor_date))
            except Exception as exc:
                print(f"[daily_pg] {secid} warning: 5m/qfq kline failed: {exc}")
        if with_orderbook:
            try:
                orderbook.crawl(secid, storage=st)
            except Exception as exc:
                print(f"[daily_pg] {secid} warning: orderbook failed: {exc}")
        if with_ticks:
            try:
                ticks.crawl(secid, storage=st)
            except Exception as exc:
                print(f"[daily_pg] {secid} warning: ticks failed: {exc}")
        if with_events:
            for cat in EVENT_CATS:
                try:
                    events.crawl(cat, secid, storage=st)
                except Exception as exc:
                    print(f"[daily_pg] {secid} warning: event {cat} failed: {exc}")
        if with_daily_kline and (day_qfq_rows <= 0 or day_none_rows <= 0):
            missing = ",".join(
                name for name, rows in (("qfq", day_qfq_rows), ("none", day_none_rows)) if rows <= 0
            )
            set_state_resilient(
                state_job, secid, cursor, "failed",
                f"empty daily kline response: {missing}", storage=st,
            )
            return "failed"
        if not set_state_resilient(state_job, secid, cursor, "ok", None, storage=st):
            return "failed"
        return "ok"
    except Exception as e:
        set_state_resilient(state_job, secid, cursor, "failed", str(e), storage=st)
        print(f"[daily_pg] {secid} 失败: {e}")
        return "failed"
    finally:
        try:
            st.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secid", action="append")
    ap.add_argument("--universe", action="store_true")
    ap.add_argument("--asset-type", action="append", choices=["stock", "index", "industry", "concept", "board"])
    ap.add_argument("--limit", type=int)
    ap.add_argument("--no-ticks", action="store_true")
    ap.add_argument("--no-orderbook", action="store_true")
    ap.add_argument("--no-events", action="store_true")
    ap.add_argument("--news", action="store_true")
    ap.add_argument("--news-limit", type=int, default=50)
    ap.add_argument("--workers", type=int, default=systemConfig.MAX_WORKERS)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--cursor-date", help="Override state cursor date YYYY-MM-DD; defaults to local today.")
    ap.add_argument("--state-job", default="daily", help="market.crawl_state job name used for skip/ok markers.")
    ap.add_argument("--no-kline-probe", action="store_true", help="Disable startup probe for Eastmoney kline API health.")
    ap.add_argument("--skip-kline", action="store_true", help="Collect non-K-line feeds only; useful during a K-line endpoint outage.")
    ap.add_argument(
        "--skip-daily-kline",
        action="store_true",
        help="Do not persist an in-progress daily candle; minute K-lines remain enabled.",
    )
    ap.add_argument("--allow-kline-failure", action="store_true", help="Exit successfully after non-K-line collection when daily K-lines are unavailable.")
    ap.add_argument("--max-seconds", type=int, default=600, help="Total primary-source request budget, including probes.")
    ap.add_argument("--health-output", type=Path, help="Local JSON report of per-feed availability and failures.")
    args = ap.parse_args()
    cursor_date = dt.date.fromisoformat(args.cursor_date) if args.cursor_date else None
    client.configure_budget(args.max_seconds, timeout=4, retries=1)
    feeds = probe_market_feeds()
    if args.skip_kline:
        feeds["daily"] = False
    summary = {"probe": feeds, "complete": False, "ok": 0, "failed": 0,
               "unattempted": 0, "cursor_date": today_cursor(cursor_date)}

    def save_summary():
        summary["circuits"] = client.circuit.snapshot()
        if args.health_output:
            args.health_output.parent.mkdir(parents=True, exist_ok=True)
            args.health_output.write_text(json.dumps(summary, indent=2))
        print("[source_health] " + json.dumps(summary), flush=True)

    if not any(feeds.values()):
        summary["reason"] = "all_primary_feeds_unavailable"
        save_summary()
        raise SystemExit(2)

    if args.news:
        st = PGStorage()
        try:
            for ch in news.CHANNELS:
                news.crawl(ch, st, limit=args.news_limit)
        finally:
            st.close()

    secids = list(args.secid or [])
    if args.universe:
        secids = load_pg_universe(args.asset_type)
    if args.limit:
        secids = secids[:args.limit]
    if not secids:
        return
    kline_available = feeds["daily"]

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        def submit(s):
            return ex.submit(
                crawl_one,
                s,
                kline_available,
                not args.skip_daily_kline,
                not args.no_ticks,
                not args.no_orderbook,
                not args.no_events,
                args.force,
                cursor_date,
                args.state_job,
                feeds,
            )
        targets = iter(secids)
        futs = {}
        for _ in range(max(1, args.workers)):
            code = next(targets, None)
            if code is not None:
                futs[submit(code)] = code
        ok_count = 0
        skipped_count = 0
        failed_count = 0
        with tqdm.tqdm(total=len(secids), desc="daily_pg") as progress:
            while futs:
                done, _ = wait(futs, return_when=FIRST_COMPLETED)
                for fut in done:
                    secid = futs.pop(fut)
                    try:
                        result = fut.result()
                    except Exception as exc:
                        print(f"[daily_pg] {secid} worker crashed: {type(exc).__name__}")
                        set_state_resilient(args.state_job, secid, today_cursor(cursor_date), "failed", type(exc).__name__)
                        result = "failed"
                    if result == "ok":
                        ok_count += 1
                    elif result == "skipped":
                        skipped_count += 1
                    else:
                        failed_count += 1
                    progress.update()
                    if client.remaining() > 0:
                        code = next(targets, None)
                        if code is not None:
                            futs[submit(code)] = code
        summary.update(ok=ok_count, failed=failed_count,
                       unattempted=len(secids)-ok_count-failed_count-skipped_count,
                       complete=ok_count == len(secids) and all(feeds.values()))
        save_summary()
        if summary["unattempted"]:
            raise SystemExit(3)
        print(
            f"[daily_pg] complete ok={ok_count} skipped={skipped_count} "
            f"failed={failed_count} total={len(secids)}"
        )
        if not args.allow_kline_failure and ok_count == 0 and (skipped_count or failed_count):
            raise SystemExit("all daily crawl targets failed or returned no kline data")
        if not args.allow_kline_failure and failed_count > max(100, len(secids) // 2):
            raise SystemExit(f"too many daily crawl failures: failed={failed_count} total={len(secids)}")


if __name__ == "__main__":
    main()
