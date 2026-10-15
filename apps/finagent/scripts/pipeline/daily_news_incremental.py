#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

from common import ROOT, crawler_run, iso, log_path, max_news_date, run_logged, today, ymd

EASTMONEY_ROOT = ROOT / "Spider/eastmoney"
if str(EASTMONEY_ROOT) not in sys.path:
    sys.path.insert(0, str(EASTMONEY_ROOT))

from eastmoney.pg_storage import PGStorage
from eastmoney.fetchers import news as eastmoney_news


PEOPLES_DB = ROOT / "Spider/PeoplesDaily/peoples_daily.sqlite3"
XINHUA_DB = ROOT / "Spider/XinhuaDaily/xinhua_daily.sqlite3"


def default_start(source_name: str, fallback: dt.date, lookback_days: int) -> dt.date:
    latest = max_news_date(source_name)
    if not latest:
        return fallback
    return min(latest, today()) - dt.timedelta(days=lookback_days)


def run_peoples(start: dt.date, end: dt.date) -> None:
    log_file = log_path("peoples_daily")
    with crawler_run("daily_news", "peoples_daily", start, end, log_file):
        run_logged(
            [
                sys.executable,
                "-u",
                "Spider/PeoplesDaily/peoples_daily_spider.py",
                "--data-dir",
                "Spider/PeoplesDaily/data",
                "--clean-dir",
                "Spider/PeoplesDaily/clean_data",
                "--db-file",
                str(PEOPLES_DB),
                "--start",
                ymd(start),
                "--end",
                ymd(end),
                "--sleep",
                "0.1",
            ],
            log_file,
        )


def run_xinhua(start: dt.date, end: dt.date) -> None:
    log_file = log_path("xinhua_daily")
    with crawler_run("daily_news", "xinhua_daily", start, end, log_file):
        run_logged(
            [
                sys.executable,
                "-u",
                "Spider/XinhuaDaily/xinhua_daily_spider.py",
                "--start",
                ymd(start),
                "--end",
                ymd(end),
                "--db-file",
                str(XINHUA_DB),
                "--data-dir",
                "Spider/XinhuaDaily/data",
                "--timeout",
                "20",
                "--sleep",
                "0.2",
            ],
            log_file,
        )


def export_xinhua_year_csv(start: dt.date, end: dt.date) -> None:
    output_dir = ROOT / "Spider/XinhuaDaily/clean_data"
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(XINHUA_DB)
    conn.row_factory = sqlite3.Row
    try:
        for year in range(start.year, end.year + 1):
            rows = conn.execute(
                """
                SELECT date, page_no, article_no, title, subtitle, shoulder, author,
                       source_line, source, page_name, article_url, content_text,
                       txt_path, raw_html_path
                FROM articles
                WHERE date BETWEEN ? AND ?
                ORDER BY date, page_no, article_no
                """,
                (f"{year}0101", f"{year}1231"),
            ).fetchall()
            path = output_dir / f"Xinhua_Daily_{year}.csv"
            with path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys() if rows else ["date"])
                writer.writeheader()
                for row in rows:
                    writer.writerow(dict(row))
    finally:
        conn.close()


def pdf_name(page_no: str, url: str) -> str:
    suffix = Path(urlparse(url).path).suffix or ".pdf"
    return f"Page{str(page_no).zfill(2)}{suffix}"


def download_page_pdfs(source: str, db_path: Path, start: dt.date, end: dt.date) -> int:
    root = ROOT / "newspaper_pdfs" / source
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT date, page_no, pdf_url
        FROM pages
        WHERE date BETWEEN ? AND ?
          AND COALESCE(pdf_url, '') != ''
        ORDER BY date, page_no
        """,
        (ymd(start), ymd(end)),
    ).fetchall()
    conn.close()

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    downloaded = 0
    for row in rows:
        date_text = str(row["date"])
        target = root / date_text[:4] / date_text / pdf_name(row["page_no"], row["pdf_url"])
        if target.exists() and target.stat().st_size > 0:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = session.get(row["pdf_url"], timeout=30)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            target.write_bytes(resp.content)
            downloaded += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] page pdf failed: {row['pdf_url']} -> {exc}")
    return downloaded


def prepare_and_import() -> None:
    log_file = log_path("news_import")
    with crawler_run("import_news", "news", log_file=log_file):
        run_logged([sys.executable, "scripts/pipeline/incremental_import.py", "--source", "news"], log_file)


def run_eastmoney_news(limit: int) -> None:
    log_file = log_path("eastmoney_news")
    with crawler_run("daily_news", "eastmoney_news", today(), today(), log_file):
        storage = PGStorage()
        try:
            for channel in eastmoney_news.CHANNELS:
                count = eastmoney_news.crawl(channel, storage, limit=limit)
                print(f"[daily_news] eastmoney_{channel} upsert={count}")
        finally:
            storage.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run People's Daily and Xinhua Daily incremental crawls in parallel.")
    parser.add_argument("--start", help="Override start date YYYYMMDD for both sources.")
    parser.add_argument("--end", help="Override end date YYYYMMDD. Defaults to today.")
    parser.add_argument("--lookback-days", type=int, default=1)
    parser.add_argument("--fallback-start", default="20230101")
    parser.add_argument("--eastmoney-news-limit", type=int, default=80)
    parser.add_argument("--skip-import", action="store_true")
    args = parser.parse_args()

    end = dt.datetime.strptime(args.end, "%Y%m%d").date() if args.end else today()
    if args.start:
        peoples_start = xinhua_start = dt.datetime.strptime(args.start, "%Y%m%d").date()
    else:
        fallback = dt.datetime.strptime(args.fallback_start, "%Y%m%d").date()
        peoples_start = default_start("peoples_daily", fallback, args.lookback_days)
        xinhua_start = default_start("xinhua_daily", fallback, args.lookback_days)

    jobs = [
        ("peoples_daily", run_peoples, peoples_start),
        ("xinhua_daily", run_xinhua, xinhua_start),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(fn, start, end): name for name, fn, start in jobs}
        for future in as_completed(futures):
            name = futures[future]
            future.result()
            print(f"[daily_news] {name} done")

    export_xinhua_year_csv(xinhua_start, end)
    peoples_pdf = download_page_pdfs("peoples_daily", PEOPLES_DB, peoples_start, end)
    xinhua_pdf = download_page_pdfs("xinhua_daily", XINHUA_DB, xinhua_start, end)
    print(f"[daily_news] page pdf downloaded: peoples_daily={peoples_pdf} xinhua_daily={xinhua_pdf}")

    if not args.skip_import:
        prepare_and_import()
    run_eastmoney_news(args.eastmoney_news_limit)
    quality_log = log_path("document_quality_v3")
    run_logged(
        [
            sys.executable, "scripts/pipeline/daily_cninfo_filings.py",
            "--start", iso(min(peoples_start, xinhua_start)), "--end", iso(end),
            "--download-financial-pdfs", "--download-corporate-action-pdfs",
        ],
        quality_log,
    )
    run_logged(
        [
            sys.executable, "scripts/analytics/extract_corporate_actions_cninfo.py",
            "--lookback-days", "14",
        ],
        quality_log,
    )
    run_logged(
        [
            sys.executable, "scripts/analytics/extract_financial_facts_cninfo.py",
            "--lookback-days", "14",
        ],
        quality_log,
    )
    run_logged(
        [
            sys.executable, "scripts/search/score_document_quality_v3.py",
            "--batch-size", "1000",
        ],
        quality_log,
    )
    print(f"[daily_news] complete through {iso(end)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
