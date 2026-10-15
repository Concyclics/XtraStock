#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path


FILENAME_RE = re.compile(r"^(?P<ymd>\d{8})-(?P<page>\d{2})-(?P<article>\d{2})\.txt$")
PEOPLES_DAILY_RE = re.compile(r"《人民日报》[（(](?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日第(?P<page>\d{1,2})版[）)]")
BYLINE_RE = re.compile(r"^(本报记者|记者|新华社记者|人民日报记者)?[\u4e00-\u9fff·、]{2,30}$")


@dataclass(frozen=True)
class ParsedArticle:
    external_id: str
    source_type: str
    source_name: str
    title: str
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
    size_bytes: int
    metadata: str


@dataclass(frozen=True)
class PageInfo:
    page_name: str = ""
    page_url: str = ""
    page_pdf_url: str = ""
    page_image_url: str = ""


def normalize_line(line: str) -> str:
    line = line.replace("\ufeff", "")
    line = line.replace("\u3000", " ")
    line = line.replace("\xa0", " ")
    return re.sub(r"\s+", " ", line).strip()


def parse_date_from_name(path: Path) -> tuple[date, str, str]:
    match = FILENAME_RE.match(path.name)
    if not match:
        raise ValueError(f"unexpected file name: {path}")
    ymd = match.group("ymd")
    return date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])), match.group("page"), match.group("article")


def find_publication_line(lines: list[str]) -> tuple[int | None, date | None, str | None]:
    for idx, line in enumerate(lines):
        match = PEOPLES_DAILY_RE.search(line)
        if match:
            parsed_date = date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
            return idx, parsed_date, match.group("page").zfill(2)
    return None, None, None


def looks_like_author(line: str) -> bool:
    if not line:
        return False
    if "《人民日报》" in line:
        return False
    if len(line) > 35:
        return False
    if any(mark in line for mark in "，。、；：！？（）()【】[]"):
        return False
    return bool(BYLINE_RE.match(line))


def load_page_info(sqlite_path: Path | None) -> dict[tuple[str, str], PageInfo]:
    if not sqlite_path or not sqlite_path.exists():
        return {}

    conn = sqlite3.connect(sqlite_path)
    try:
        rows = conn.execute(
            """
            SELECT date, page_no, page_name, page_url, pdf_url, image_url
            FROM pages
            """
        ).fetchall()
    finally:
        conn.close()

    return {
        (str(date_value), str(page_no).zfill(2)): PageInfo(
            page_name=page_name or "",
            page_url=page_url or "",
            page_pdf_url=pdf_url or "",
            page_image_url=image_url or "",
        )
        for date_value, page_no, page_name, page_url, pdf_url, image_url in rows
    }


def parse_article(path: Path, root: Path, page_info_by_key: dict[tuple[str, str], PageInfo]) -> ParsedArticle:
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    normalized_lines = [normalize_line(line) for line in raw_text.splitlines()]
    nonempty = [line for line in normalized_lines if line]

    file_date, file_page, file_article = parse_date_from_name(path)
    pub_idx, pub_date, pub_page = find_publication_line(normalized_lines)

    title = nonempty[0] if nonempty else path.stem
    subtitle = ""
    author = ""
    body_start_idx = 1

    if pub_idx is not None:
        before_pub = [line for line in normalized_lines[:pub_idx] if line]
        title = before_pub[0] if before_pub else title
        if len(before_pub) >= 2 and looks_like_author(before_pub[-1]):
            author = before_pub[-1]
            if len(before_pub) >= 3:
                subtitle = " ".join(before_pub[1:-1])
        elif len(before_pub) >= 2:
            subtitle = " ".join(before_pub[1:])
        body_start_idx = pub_idx + 1
    else:
        if (
            len(nonempty) >= 2
            and len(nonempty[1]) <= 120
            and not nonempty[1].startswith("本报")
            and not nonempty[1].startswith("新华社")
            and not nonempty[1].startswith("■")
        ):
            subtitle = nonempty[1]
            body_start_idx = 2

    body_lines = [line for line in normalized_lines[body_start_idx:] if line and "《人民日报》" not in line]
    content_text = "\n".join(body_lines)
    if not content_text:
        content_text = "\n".join(nonempty[1:]) if len(nonempty) > 1 else title

    paper_date = pub_date or file_date
    page_no = pub_page or file_page
    page_info = page_info_by_key.get((paper_date.strftime("%Y%m%d"), page_no), PageInfo())
    rel_path = str(path.relative_to(root.parent)) if path.is_relative_to(root.parent) else str(path)
    digest = hashlib.sha256(raw_text.encode("utf-8", errors="replace")).hexdigest()

    metadata = {
        "file_name": path.name,
        "file_stem": path.stem,
        "subtitle": subtitle,
        "page_name": page_info.page_name,
        "page_url": page_info.page_url,
        "page_pdf_url": page_info.page_pdf_url,
        "page_image_url": page_info.page_image_url,
        "parser_version": "peoples_daily_v1",
        "source_path_relative_to_codes": rel_path,
    }

    return ParsedArticle(
        external_id=path.stem,
        source_type="peoples_daily",
        source_name="peoples_daily",
        title=title,
        author=author,
        publisher="人民日报",
        published_at=paper_date.isoformat(),
        url="",
        language="zh",
        content_text=content_text,
        raw_text=raw_text,
        content_hash=digest,
        newspaper_date=paper_date.isoformat(),
        page_no=page_no,
        article_no=file_article,
        page_name=page_info.page_name,
        page_url=page_info.page_url,
        page_pdf_url=page_info.page_pdf_url,
        page_image_url=page_info.page_image_url,
        section_name=page_info.page_name,
        original_file_path=str(path),
        size_bytes=path.stat().st_size,
        metadata=json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
    )


def open_tsv(output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(ParsedArticle.__dataclass_fields__.keys())
    f = output_path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(f, fieldnames=fields, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    return f, writer


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare People's Daily TSV staging file for PostgreSQL COPY.")
    parser.add_argument("--input-dir", default="Spider/PeoplesDaily/data", help="Directory containing People's Daily txt files.")
    parser.add_argument("--output", default="build/peoples_daily_import/peoples_daily_articles.tsv", help="Output TSV path.")
    parser.add_argument("--page-db", default="Spider/PeoplesDaily/peoples_daily.sqlite3", help="SQLite database containing pages metadata.")
    parser.add_argument("--limit", type=int, default=0, help="Limit files for dry runs.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    output_path = Path(args.output).resolve()
    page_db = Path(args.page_db).resolve() if args.page_db else None
    page_info_by_key = load_page_info(page_db)
    files = sorted(input_dir.glob("*.txt"))
    if args.limit:
        files = files[: args.limit]

    skipped: list[str] = []
    prepared_rows = 0
    handle, writer = open_tsv(output_path)
    try:
        for idx, path in enumerate(files, start=1):
            try:
                article = parse_article(path, input_dir, page_info_by_key)
                writer.writerow(article.__dict__)
                prepared_rows += 1
            except Exception as exc:
                skipped.append(f"{path}\t{exc}")
            if idx % 10000 == 0:
                print(f"processed={idx} prepared={prepared_rows} skipped={len(skipped)}")
    finally:
        handle.close()

    report_path = output_path.with_suffix(".report.json")
    report_path.write_text(
        json.dumps(
            {
                "input_dir": str(input_dir),
                "output": str(output_path),
                "page_db": str(page_db) if page_db else "",
                "page_info_rows": len(page_info_by_key),
                "total_files": len(files),
                "prepared_rows": prepared_rows,
                "skipped_rows": len(skipped),
                "skipped_samples": skipped[:20],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"prepared_rows={prepared_rows} skipped_rows={len(skipped)} output={output_path}")
    print(f"report={report_path}")


if __name__ == "__main__":
    main()
