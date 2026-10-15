#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class ParsedArticle:
    external_id: str
    source_type: str
    source_name: str
    title: str
    subtitle: str
    author: str
    publisher: str
    published_at: str
    url: str
    language: str
    content_text: str
    raw_text: str
    content_hash: str
    newspaper_date: str
    page_no: str
    article_no: str
    page_name: str
    page_url: str
    page_pdf_url: str
    page_image_url: str
    section_name: str
    original_file_path: str
    raw_html_file_path: str
    size_bytes: int
    raw_html_size_bytes: int
    metadata: str


def parse_ymd(value: str) -> str:
    value = str(value)
    return date(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat()


def read_text_file(path_value: str | None) -> str:
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def file_size(path_value: str | None) -> int:
    if not path_value:
        return 0
    path = Path(path_value)
    return path.stat().st_size if path.exists() else 0


def normalize_source(value: str | None) -> str:
    value = (value or "").strip()
    return value or "新华每日电讯"


def parse_article(row: sqlite3.Row) -> ParsedArticle:
    txt_path = row["txt_path"] or ""
    raw_html_path = row["raw_html_path"] or ""
    raw_text = read_text_file(txt_path) or (row["content_text"] or "")
    content_text = row["content_text"] or raw_text
    digest = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()
    published_at = parse_ymd(row["date"])
    page_name = row["article_page_name"] or row["page_page_name"] or ""
    page_url = row["page_url"] or ""
    page_pdf_url = row["pdf_url"] or ""
    page_image_url = row["image_url"] or ""
    publisher = "新华每日电讯"

    metadata = {
        "parser_version": "xinhua_daily_v1",
        "source": normalize_source(row["source"]),
        "source_line": row["source_line"] or "",
        "shoulder": row["shoulder"] or "",
        "sort_order": row["sort_order"],
        "layout_box": json.loads(row["layout_box_json"] or "{}"),
        "field_id": row["field_id"] or "",
        "page_editor": row["editor"] or "",
        "page_raw_html_path": row["page_raw_html_path"] or "",
        "article_raw_html_path": raw_html_path,
        "source_path_relative_to_codes": txt_path,
        "page_name": page_name,
        "page_url": page_url,
        "page_pdf_url": page_pdf_url,
        "page_image_url": page_image_url,
        "fetched_at": row["fetched_at"] or "",
    }

    return ParsedArticle(
        external_id=row["article_id"],
        source_type="news",
        source_name="xinhua_daily",
        title=row["title"] or row["article_id"],
        subtitle=row["subtitle"] or "",
        author=row["author"] or "",
        publisher=publisher,
        published_at=published_at,
        url=row["article_url"] or "",
        language="zh",
        content_text=content_text,
        raw_text=raw_text,
        content_hash=digest,
        newspaper_date=published_at,
        page_no=str(row["page_no"] or "").zfill(2),
        article_no=str(row["article_no"] or ""),
        page_name=page_name,
        page_url=page_url,
        page_pdf_url=page_pdf_url,
        page_image_url=page_image_url,
        section_name=page_name,
        original_file_path=txt_path,
        raw_html_file_path=raw_html_path,
        size_bytes=file_size(txt_path),
        raw_html_size_bytes=file_size(raw_html_path),
        metadata=json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
    )


def open_tsv(output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(ParsedArticle.__dataclass_fields__.keys())
    handle = output_path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    return handle, writer


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Xinhua Daily TSV staging file for PostgreSQL COPY.")
    parser.add_argument("--db-file", default="Spider/XinhuaDaily/xinhua_daily.sqlite3", help="Xinhua Daily SQLite database.")
    parser.add_argument("--output", default="build/xinhua_daily_import/xinhua_daily_articles.tsv", help="Output TSV path.")
    parser.add_argument("--limit", type=int, default=0, help="Limit rows for dry runs.")
    args = parser.parse_args()

    db_file = Path(args.db_file).resolve()
    output_path = Path(args.output).resolve()
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    query = """
        SELECT
          a.article_id,
          a.date,
          a.page_no,
          a.article_no,
          a.sort_order,
          a.title,
          a.subtitle,
          a.shoulder,
          a.author,
          a.source_line,
          a.source,
          a.page_name AS article_page_name,
          a.article_url,
          a.content_text,
          a.txt_path,
          a.raw_html_path,
          a.layout_box_json,
          a.field_id,
          a.fetched_at,
          p.page_name AS page_page_name,
          p.page_url,
          p.pdf_url,
          p.image_url,
          p.editor,
          p.raw_html_path AS page_raw_html_path
        FROM articles a
        LEFT JOIN pages p
          ON p.date = a.date
         AND p.page_no = a.page_no
        ORDER BY a.date, a.page_no, a.article_no
    """
    if args.limit:
        query += " LIMIT ?"
        rows = conn.execute(query, (args.limit,))
    else:
        rows = conn.execute(query)

    prepared_rows = 0
    skipped: list[str] = []
    handle, writer = open_tsv(output_path)
    try:
        for row in rows:
            try:
                writer.writerow(parse_article(row).__dict__)
                prepared_rows += 1
            except Exception as exc:
                skipped.append(f"{row['article_id']}\t{exc}")
            if prepared_rows and prepared_rows % 10000 == 0:
                print(f"prepared={prepared_rows} skipped={len(skipped)}")
    finally:
        handle.close()
        conn.close()

    report_path = output_path.with_suffix(".report.json")
    report_path.write_text(
        json.dumps(
            {
                "db_file": str(db_file),
                "output": str(output_path),
                "prepared_rows": prepared_rows,
                "skipped_rows": len(skipped),
                "skipped_samples": skipped[:20],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"done prepared={prepared_rows} skipped={len(skipped)} output={output_path}")
    print(f"report={report_path}")


if __name__ == "__main__":
    main()
