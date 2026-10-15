#!/usr/bin/env python3
"""Transactional source-version ingestion, without full-history TSV exports.

The ledger and business writes commit together. A failed batch stays retryable;
unchanged inputs require only metadata reads. --verify-content also hashes files.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from psycopg.errors import LockNotAvailable, DeadlockDetected, SerializationFailure

from common import ROOT, connect, pipeline_lock

sys.path.insert(0, str(ROOT / "scripts"))
import prepare_eastmoney_reports_import as reports
import prepare_peoples_daily_import as peoples
import prepare_xinhua_daily_import as xinhua

SOURCES = {
    "eastmoney": ("eastmoney_reports", reports.PreparedReport),
    "peoples": ("peoples_daily_articles", peoples.ParsedArticle),
    "xinhua": ("xinhua_daily_articles", xinhua.ParsedArticle),
}
VERSION = "incremental-source-v1"


def fingerprint(row, paths, verify_content=False):
    files = []
    for value in paths:
        if not value:
            files.append(None)
            continue
        path = Path(value)
        try:
            stat = path.stat()
        except FileNotFoundError:
            files.append([str(path), "missing"])
            continue
        signature = [str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]
        if verify_content:
            with path.open("rb") as handle:
                signature.append(hashlib.file_digest(handle, "sha256").hexdigest())
        files.append(signature)
    payload = json.dumps([VERSION, row, files], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def source_items(source):
    if source == "eastmoney":
        with sqlite3.connect(f"file:{ROOT / 'eastmoney_reports.sqlite3'}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            for kind, table in (("stock", "stock_reports"), ("industry", "industry_reports")):
                for row in reports.rows(db, table):
                    paths = list(reports.resolve_paths(kind, row).values())
                    key = kind + ":" + row["报告链接"]
                    yield key, dict(row), paths, lambda r=row, k=kind: reports.prepare_report(k, r)
    elif source == "peoples":
        directory = ROOT / "Spider/PeoplesDaily/data"
        page_info = peoples.load_page_info(ROOT / "Spider/PeoplesDaily/peoples_daily.sqlite3")
        for path in sorted(directory.glob("*.txt")):
            date, page, _ = peoples.parse_date_from_name(path)
            page_value = page_info.get((date.strftime("%Y%m%d"), page), peoples.PageInfo())
            yield path.name, dataclasses.asdict(page_value), [path], lambda p=path: peoples.parse_article(p, directory, page_info)
    else:
        with sqlite3.connect(f"file:{ROOT / 'Spider/XinhuaDaily/xinhua_daily.sqlite3'}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            query = """SELECT a.*,a.page_name AS article_page_name,p.page_name AS page_page_name,
                       p.page_url,p.pdf_url,p.image_url,p.editor,p.raw_html_path AS page_raw_html_path
                       FROM articles a LEFT JOIN pages p ON p.date=a.date AND p.page_no=a.page_no
                       ORDER BY a.date,a.page_no,a.article_no"""
            for row in db.execute(query):
                paths = [row["txt_path"], row["raw_html_path"]]
                yield row["article_id"], dict(row), paths, lambda r=row: xinhua.parse_article(r)


def sql_for(source):
    table, _ = SOURCES[source]
    stem = {"eastmoney": "eastmoney_reports", "peoples": "peoples_daily", "xinhua": "xinhua_daily"}[source]
    schema = (ROOT / f"docker/postgres/import_{stem}_setup.sql").read_text()
    match = re.search(r"CREATE TABLE staging\." + table + r"\s*(\([\s\S]+?\));", schema)
    if not match:
        raise RuntimeError("unsupported staging schema")
    create = f"CREATE TEMP TABLE {table} " + match.group(1) + " ON COMMIT DROP;"
    materialize = (ROOT / f"docker/postgres/import_{stem}_materialize.sql").read_text()
    # Full-history loaders append global ANALYZE/count reports after COMMIT.
    # A 500-row incremental batch must not rescan the entire corpus each time.
    materialize = re.split(r"(?m)^COMMIT;\s*$",materialize,maxsplit=1)[0]
    materialize = re.sub(r"(?m)^\s*BEGIN;\s*$", "", materialize)
    if source in ('peoples','xinhua'):
        # Separate statements see newly inserted document IDs. A sibling CTE
        # reading corpus.documents cannot see another modifying CTE's inserts.
        materialize=materialize.replace('WITH upserted_documents AS (','',1)
        materialize=materialize.replace('  RETURNING id, source_type, source_name, external_id\n),\nall_documents AS (',
                                        ';\nWITH all_documents AS (',1)
    materialize = materialize.replace("staging." + table, "pg_temp." + table)
    materialize = materialize.replace("CREATE TEMP TABLE eastmoney_reports_dedup AS", "CREATE TEMP TABLE eastmoney_reports_dedup ON COMMIT DROP AS")
    return create, materialize


def pg_value(value):
    # PostgreSQL text cannot represent NUL. Keep a visible replacement character;
    # source files stay untouched and affected source keys/fields are logged.
    if value == "": return None
    return value.replace("\x00", "\ufffd") if isinstance(value,str) else value


def write_batch(conn, source, batch):
    for attempt in range(3):
        try:
            return write_batch_once(conn,source,batch)
        except (LockNotAvailable,DeadlockDetected,SerializationFailure) as exc:
            # The transaction context has rolled the entire batch back. Retry
            # only transient database conflicts, with a fixed attempt budget.
            print(json.dumps({'source':source,'event':'batch_transaction_retry','attempt':attempt+1,
                'error_class':type(exc).__name__,'batch_size':len(batch)}),flush=True)
            if attempt==2: raise
            time.sleep(2**attempt)


def write_batch_once(conn, source, batch):
    table, cls = SOURCES[source]
    columns = list(cls.__dataclass_fields__)
    create, materialize = sql_for(source)
    with conn.transaction():
        conn.execute("SET LOCAL lock_timeout='10s'")
        conn.execute("SET LOCAL statement_timeout='120s'")
        conn.execute(create)
        with conn.cursor().copy(f"COPY pg_temp.{table} ({','.join(columns)}) FROM STDIN") as copy:
            for key, signature, item in batch:
                # Empty numeric/date fields in the TSV loader were SQL NULL.
                nul_fields = {name: getattr(item,name).count("\x00") for name in columns
                              if isinstance(getattr(item,name),str) and "\x00" in getattr(item,name)}
                if nul_fields:
                    print(json.dumps({"source":source,"source_key":key,"normalization":"NUL to Unicode replacement character","fields":nul_fields},ensure_ascii=False),flush=True)
                copy.write_row(tuple(pg_value(getattr(item,name)) for name in columns))
        conn.execute(materialize)
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO ops.source_import_versions(source_name,source_key,input_version,content_hash)
                               VALUES(%s,%s,%s,%s) ON CONFLICT(source_name,source_key) DO UPDATE
                               SET input_version=excluded.input_version,content_hash=excluded.content_hash,imported_at=now()""",
                            [(source, key, signature, item.content_hash) for key, signature, item in batch])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=[*SOURCES, "news"], required=True)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--verify-content", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    summary = []
    for source in (["peoples", "xinhua"] if args.source == "news" else [args.source]):
        started = time.monotonic()
        with pipeline_lock("incremental_import:" + source), connect(admin=True) as conn:
            conn.autocommit = True
            if not args.dry_run:
                conn.execute("""CREATE TABLE IF NOT EXISTS ops.source_import_versions(
                         source_name text NOT NULL,source_key text NOT NULL,input_version text NOT NULL,
                         content_hash text,imported_at timestamptz NOT NULL DEFAULT now(),
                         PRIMARY KEY(source_name,source_key))""")
            exists = conn.execute("SELECT to_regclass('ops.source_import_versions') present").fetchone()["present"]
            known = {r["source_key"]: r["input_version"] for r in conn.execute(
                "SELECT source_key,input_version FROM ops.source_import_versions WHERE source_name=%s", (source,))} if exists else {}
            unchanged = imported = seen = 0
            batch = []
            for key, row, paths, prepare in source_items(source):
                seen += 1
                signature = fingerprint(row, paths, args.verify_content)
                if not args.verify_content and known.get(key) == signature:
                    unchanged += 1
                else:
                    item = prepare()  # Errors fail the job, never silently skip.
                    if signature != fingerprint(row, paths, args.verify_content):
                        raise RuntimeError("source changed during import: " + key)
                    batch.append((key, fingerprint(row, paths), item))
                    if len(batch) >= max(1, args.batch_size):
                        if not args.dry_run:
                            write_batch(conn, source, batch)
                        imported += len(batch); batch = []
                        print(json.dumps({"source": source, "committed": imported, "unchanged": unchanged}), flush=True)
                if args.limit and seen >= args.limit:
                    break
            if batch:
                if not args.dry_run:
                    write_batch(conn, source, batch)
                imported += len(batch)
            summary.append({"source": source, "scanned": seen, "changed": imported,
                            "unchanged": unchanged, "dry_run": args.dry_run,
                            "seconds": round(time.monotonic()-started, 3)})
    print(json.dumps(summary), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
