#!/usr/bin/env python3
"""Crawler for Xinhua Daily Telegraph (新华每日电讯) pages and articles.

The site has at least two archive templates:
- newer pages use UTF-8 and article hotspots in ``daoxiang`` attributes;
- older pages use GB2312 and article hotspots / title lists with ``href``.

This crawler stores issue, page, and article metadata in SQLite while also
writing article text files compatible with the existing People's Daily data
layout.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import bs4
import requests


BASE_URL = "http://mrdx.cn/content/"
DATE_LIST_URL = urljoin(BASE_URL, "datelist.aspx")
SOURCE_NAME = "新华每日电讯"
ISSUE_SUFFIXES = ("DK", "BC", "HO")
PAGE_URL_RE = re.compile(r"Page(?P<page>\d{2})(?P<suffix>[A-Z]+)\.htm", re.I)
ARTICLE_URL_RE = re.compile(r"Articel(?P<page>\d{2})(?P<article>\d{3})(?P<suffix>[A-Z]+)\.htm", re.I)
DATE_VALUE_RE = re.compile(r'"(?P<date>\d{8})"')
PAGE_TITLE_RE = re.compile(r"(?P<page>\d{1,2})\s*(?:版|:|：)\s*[:：]?\s*(?P<name>.+)")
SOURCE_LINE_RE = re.compile(r"（\s*(?P<date>\d{4}[-年]\d{1,2}[-月]\d{1,2}日?)\s*）\s*稿件来源[:：]\s*(?P<source>.+)")
STYLE_BOX_RE = re.compile(r"(?P<key>width|height|left|top)\s*:\s*(?P<value>-?\d+(?:\.\d+)?)px", re.I)
NAV_LABELS = {"上一版", "下一版", "完整版面", "PDF版", "PDF 版"}

SQL_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS issues (
    date TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    issue_url TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    page_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    page_no TEXT NOT NULL,
    page_name TEXT,
    page_url TEXT NOT NULL,
    pdf_url TEXT,
    image_url TEXT,
    editor TEXT,
    raw_html_path TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE(date, page_no)
);

CREATE TABLE IF NOT EXISTS articles (
    article_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    page_no TEXT NOT NULL,
    article_no TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    title TEXT,
    subtitle TEXT,
    shoulder TEXT,
    author TEXT,
    source_line TEXT,
    source TEXT,
    page_name TEXT,
    article_url TEXT NOT NULL,
    content_text TEXT,
    txt_path TEXT,
    raw_html_path TEXT,
    layout_box_json TEXT,
    field_id TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE(date, page_no, article_no)
);

CREATE INDEX IF NOT EXISTS idx_xinhua_pages_date ON pages(date);
CREATE INDEX IF NOT EXISTS idx_xinhua_articles_date ON articles(date);
CREATE INDEX IF NOT EXISTS idx_xinhua_articles_page ON articles(date, page_no);
"""


@dataclass(frozen=True)
class PageMeta:
    date: str
    page_no: str
    page_name: str
    page_url: str
    pdf_url: str
    image_url: str

    @property
    def page_id(self) -> str:
        return f"{self.date}-{self.page_no}"


@dataclass(frozen=True)
class ArticleLink:
    date: str
    page_no: str
    article_no: str
    sort_order: int
    title: str
    article_url: str
    layout_box: dict[str, float]
    field_id: str

    @property
    def article_id(self) -> str:
        return f"{self.date}-{self.page_no}-{self.article_no}"


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def parse_date(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y%m%d").date()


def date_range(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)


def clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def safe_filename(value: str, max_bytes: int = 120) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_") or "untitled"
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= max_bytes:
        return cleaned
    truncated = encoded[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8").rstrip("_")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return "untitled"


def decode_response(response: requests.Response) -> str:
    # The archive sometimes serves UTF-8 pages with a misleading default
    # response encoding. Prefer the detected page encoding over headers.
    encoding = response.apparent_encoding or response.encoding or "utf-8"
    try:
        return response.content.decode(encoding, errors="replace")
    except LookupError:
        return response.text


def fetch_html(
    session: requests.Session,
    url: str,
    timeout: int,
    retries: int = 2,
    quiet_404: bool = False,
) -> str | None:
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
    }
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        response: requests.Response | None = None
        try:
            response = session.get(url, headers=headers, timeout=(timeout, timeout), stream=True)
            if response.status_code == 404:
                if not quiet_404:
                    print(f"404: {url}")
                return None
            response.raise_for_status()
            chunks: list[bytes] = []
            deadline = time.monotonic() + max(timeout * 2, 10)
            for chunk in response.iter_content(65536):
                if chunk:
                    chunks.append(chunk)
                if time.monotonic() > deadline:
                    raise requests.Timeout(f"read deadline exceeded after {max(timeout * 2, 10)}s")
            response._content = b"".join(chunks)
            return decode_response(response)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(2**attempt, 5))
                continue
        finally:
            if response is not None:
                response.close()
    print(f"访问失败: {url} ({last_error})")
    return None


def fetch_available_dates(session: requests.Session, timeout: int) -> set[str]:
    html = fetch_html(session, DATE_LIST_URL, timeout, retries=4)
    if not html:
        print(f"未能读取出版日期列表，回退为逐日探测: {DATE_LIST_URL}")
        return set()

    dates = {match.group("date") for match in DATE_VALUE_RE.finditer(html)}
    if dates:
        print(f"出版日期列表 loaded={len(dates)} min={min(dates)} max={max(dates)}")
    else:
        print(f"出版日期列表为空，回退为逐日探测: {DATE_LIST_URL}")
    return dates


def init_db(db_file: Path) -> sqlite3.Connection:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.executescript(SQL_SCHEMA)
    return conn


def save_raw(data_dir: Path, date: str, kind: str, name: str, html: str) -> Path:
    raw_dir = data_dir / "raw" / date / kind
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / safe_filename(name)
    path.write_text(html, encoding="utf-8")
    return path


def resolve_issue(session: requests.Session, day: dt.date, timeout: int) -> tuple[str, str] | None:
    ymd = day.strftime("%Y%m%d")
    candidates = [f"{BASE_URL}{ymd}/Page01{suffix}.htm" for suffix in ISSUE_SUFFIXES]
    for url in candidates:
        html = fetch_html(session, url, timeout, quiet_404=True)
        if html and ("新华每日电讯" in html or "PageDate" in html):
            return url, html
    print(f"[{ymd}] 首页候选均未找到: {', '.join(f'Page01{suffix}.htm' for suffix in ISSUE_SUFFIXES)}")
    return None


def parse_page_label(text: str, fallback_no: str) -> tuple[str, str]:
    text = clean_text(text)
    if not text or text in NAV_LABELS or text == fallback_no or text == str(int(fallback_no)):
        return fallback_no, ""
    match = PAGE_TITLE_RE.search(text)
    if not match:
        return fallback_no, text
    page_no = match.group("page").zfill(2)
    page_name = clean_text(match.group("name"))
    return page_no, page_name


def constructed_pdf_url(page_url: str, date: str, page_no: str) -> str:
    return urljoin(page_url, f"../../PDF/{date}/{page_no}.pdf")


def parse_issue_pages(issue_url: str, html: str, date: str) -> list[PageMeta]:
    soup = bs4.BeautifulSoup(html, "html.parser")
    pages: dict[str, PageMeta] = {}

    # New template: visual navigation contains page title and image.
    for link in soup.select(".shijuedaohang a"):
        href = link.get("href", "")
        match = PAGE_URL_RE.search(href)
        if not match:
            continue
        if match.group("suffix").upper() == "ZB":
            continue
        page_no = match.group("page")
        label = ""
        h4 = link.find("h4")
        if h4:
            label = h4.get_text(" ", strip=True)
        page_no, page_name = parse_page_label(label, page_no)
        img = link.find("img")
        image_url = urljoin(issue_url, img.get("src")) if img and img.get("src") else ""
        page_url = urljoin(issue_url, href)
        pages[page_no] = PageMeta(date, page_no, page_name, page_url, constructed_pdf_url(page_url, date, page_no), image_url)

    # Old template and fallback: all PageXX*.htm links.
    for link in soup.find_all("a"):
        href = link.get("href", "")
        match = PAGE_URL_RE.search(href)
        if not match:
            continue
        if match.group("suffix").upper() == "ZB":
            continue
        page_no = match.group("page")
        label = link.get_text(" ", strip=True)
        page_no, page_name = parse_page_label(label, page_no)
        if page_no in pages:
            current = pages[page_no]
            if page_name and not current.page_name:
                pages[page_no] = PageMeta(date, page_no, page_name, current.page_url, current.pdf_url, current.image_url)
            continue
        page_url = urljoin(issue_url, href)
        pages[page_no] = PageMeta(date, page_no, page_name, page_url, constructed_pdf_url(page_url, date, page_no), "")

    return [pages[key] for key in sorted(pages)]


def parse_style_box(style: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for match in STYLE_BOX_RE.finditer(style or ""):
        result[match.group("key").lower()] = float(match.group("value"))
    return result


def title_from_onmousemove(value: str) -> str:
    match = re.search(r"innerText\s*=\s*'([^']+)'", value or "")
    return clean_text(match.group(1)) if match else ""


def parse_page_articles(page: PageMeta, html: str) -> tuple[str, str, list[ArticleLink]]:
    soup = bs4.BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    editor_match = re.search(r"责任编辑[:：]\s*([^\n]+)", text)
    editor = clean_text(editor_match.group(1)) if editor_match else ""

    image_url = page.image_url
    if not image_url:
        image = soup.find("img", src=re.compile(rf"IMAGE/{page.date}/{page.page_no}/Page", re.I))
        if image and image.get("src"):
            image_url = urljoin(page.page_url, image.get("src"))

    articles: dict[str, ArticleLink] = {}
    sort_order = 1

    for link in soup.find_all("a", class_="overlink"):
        href = link.get("daoxiang") or link.get("href") or ""
        match = ARTICLE_URL_RE.search(href)
        if not match:
            continue
        article_no = match.group("article")
        title = clean_text(link.get("title") or title_from_onmousemove(link.get("onmousemove", "")))
        articles[article_no] = ArticleLink(
            date=page.date,
            page_no=match.group("page"),
            article_no=article_no,
            sort_order=sort_order,
            title=title,
            article_url=urljoin(page.page_url, href),
            layout_box=parse_style_box(link.get("style", "")),
            field_id=link.get("field", ""),
        )
        sort_order += 1

    # Old template title list fallback.
    for link in soup.find_all("a", class_="atitle"):
        href = link.get("href", "")
        match = ARTICLE_URL_RE.search(href)
        if not match:
            continue
        article_no = match.group("article")
        if article_no in articles:
            continue
        articles[article_no] = ArticleLink(
            date=page.date,
            page_no=match.group("page"),
            article_no=article_no,
            sort_order=sort_order,
            title=clean_text(link.get_text(" ", strip=True)),
            article_url=urljoin(page.page_url, href),
            layout_box={},
            field_id="",
        )
        sort_order += 1

    return editor, image_url, [articles[key] for key in sorted(articles)]


def remove_noise(root: bs4.Tag) -> None:
    for tag in root.find_all(["script", "style", "meta", "link"]):
        tag.decompose()
    for hidden in root.find_all(style=re.compile(r"display\s*:\s*none", re.I)):
        hidden.decompose()


def parse_article(html: str, fallback_title: str) -> dict[str, str]:
    soup = bs4.BeautifulSoup(html, "html.parser")

    subtitle = clean_text(soup.find("h3").get_text(" ", strip=True)) if soup.find("h3") else ""
    title = clean_text(soup.find("h2").get_text(" ", strip=True)) if soup.find("h2") else ""
    shoulder = clean_text(soup.find("h4").get_text(" ", strip=True)) if soup.find("h4") else ""
    if not title:
        title_tag = soup.find("title")
        if title_tag:
            title_parts = [clean_text(part) for part in title_tag.get_text(" ", strip=True).split("--") if clean_text(part)]
            title = title_parts[-1] if title_parts else ""
    if not title:
        title = fallback_title

    content_root = soup.find(id="contenttext") or soup.find(id="oldcontenttext")
    if content_root is None:
        content_root = soup.body or soup
    remove_noise(content_root)
    raw_text = content_root.get_text("\n", strip=True)
    lines = [clean_text(line) for line in raw_text.splitlines() if clean_text(line)]

    source_line = ""
    source = SOURCE_NAME
    for line in lines:
        match = SOURCE_LINE_RE.search(line)
        if match:
            source_line = clean_text(line)
            source = clean_text(match.group("source"))
            break

    # Remove duplicated chrome/title/source lines from body, but keep author lines.
    filtered: list[str] = []
    seen_title = False
    for line in lines:
        if source_line and line == source_line:
            continue
        if title and line == title and not seen_title:
            seen_title = True
            continue
        if subtitle and line == subtitle:
            continue
        if line in {"上一篇", "下一篇", SOURCE_NAME}:
            continue
        filtered.append(line)
    content_text = "\n".join(filtered).strip()

    author = ""
    if filtered:
        first = filtered[0]
        if first.startswith("本报") or "记者" in first[:20]:
            author = first

    return {
        "title": title,
        "subtitle": subtitle,
        "shoulder": shoulder,
        "author": author,
        "source_line": source_line,
        "source": source,
        "content_text": content_text,
    }


def upsert_issue(conn: sqlite3.Connection, date: str, issue_url: str, page_count: int) -> None:
    conn.execute(
        """
        INSERT INTO issues(date, source, issue_url, page_count, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            issue_url=excluded.issue_url,
            page_count=excluded.page_count,
            fetched_at=excluded.fetched_at
        """,
        (date, SOURCE_NAME, issue_url, page_count, now_iso()),
    )


def upsert_page(
    conn: sqlite3.Connection,
    page: PageMeta,
    editor: str,
    image_url: str,
    raw_html_path: Path,
) -> None:
    conn.execute(
        """
        INSERT INTO pages(page_id, date, page_no, page_name, page_url, pdf_url, image_url, editor, raw_html_path, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            page_name=excluded.page_name,
            page_url=excluded.page_url,
            pdf_url=excluded.pdf_url,
            image_url=excluded.image_url,
            editor=excluded.editor,
            raw_html_path=excluded.raw_html_path,
            fetched_at=excluded.fetched_at
        """,
        (
            page.page_id,
            page.date,
            page.page_no,
            page.page_name,
            page.page_url,
            page.pdf_url,
            image_url or page.image_url,
            editor,
            str(raw_html_path),
            now_iso(),
        ),
    )


def upsert_article(
    conn: sqlite3.Connection,
    link: ArticleLink,
    parsed: dict[str, str],
    page_name: str,
    txt_path: Path,
    raw_html_path: Path,
) -> None:
    conn.execute(
        """
        INSERT INTO articles(
            article_id, date, page_no, article_no, sort_order, title, subtitle, shoulder,
            author, source_line, source, page_name, article_url, content_text, txt_path,
            raw_html_path, layout_box_json, field_id, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_id) DO UPDATE SET
            sort_order=excluded.sort_order,
            title=excluded.title,
            subtitle=excluded.subtitle,
            shoulder=excluded.shoulder,
            author=excluded.author,
            source_line=excluded.source_line,
            source=excluded.source,
            page_name=excluded.page_name,
            article_url=excluded.article_url,
            content_text=excluded.content_text,
            txt_path=excluded.txt_path,
            raw_html_path=excluded.raw_html_path,
            layout_box_json=excluded.layout_box_json,
            field_id=excluded.field_id,
            fetched_at=excluded.fetched_at
        """,
        (
            link.article_id,
            link.date,
            link.page_no,
            link.article_no,
            link.sort_order,
            parsed["title"],
            parsed["subtitle"],
            parsed["shoulder"],
            parsed["author"],
            parsed["source_line"],
            parsed["source"],
            page_name,
            link.article_url,
            parsed["content_text"],
            str(txt_path),
            str(raw_html_path),
            json.dumps(link.layout_box, ensure_ascii=False, sort_keys=True),
            link.field_id,
            now_iso(),
        ),
    )


def article_exists(conn: sqlite3.Connection, article_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM articles WHERE article_id = ?", (article_id,)).fetchone()
    return row is not None


def write_article_txt(data_dir: Path, link: ArticleLink, parsed: dict[str, str]) -> Path:
    txt_dir = data_dir / "txt"
    txt_dir.mkdir(parents=True, exist_ok=True)
    title = safe_filename(parsed["title"] or link.title)
    path = txt_dir / f"{link.article_id}-{title}.txt"
    parts = [
        parsed["subtitle"],
        parsed["title"],
        parsed["shoulder"],
        parsed["source_line"],
        parsed["content_text"],
    ]
    path.write_text("\n".join(part for part in parts if part).strip() + "\n", encoding="utf-8")
    return path


def crawl_day(
    conn: sqlite3.Connection,
    session: requests.Session,
    day: dt.date,
    data_dir: Path,
    timeout: int,
    sleep_sec: float,
    force: bool,
) -> tuple[int, int]:
    date = day.strftime("%Y%m%d")
    issue = resolve_issue(session, day, timeout)
    if issue is None:
        print(f"[{date}] 未找到首页")
        return 0, 0

    issue_url, issue_html = issue
    save_raw(data_dir, date, "issue", "issue.html", issue_html)
    pages = parse_issue_pages(issue_url, issue_html, date)
    upsert_issue(conn, date, issue_url, len(pages))
    print(f"[{date}] pages={len(pages)} {issue_url}")

    article_count = 0
    for page in pages:
        page_html = fetch_html(session, page.page_url, timeout)
        if not page_html:
            continue
        raw_page_path = save_raw(data_dir, date, "pages", f"{page.page_no}.html", page_html)
        editor, image_url, article_links = parse_page_articles(page, page_html)
        upsert_page(conn, page, editor, image_url, raw_page_path)
        print(f"[{date}] page {page.page_no} {page.page_name or '-'} articles={len(article_links)}")

        for link in article_links:
            if not force and article_exists(conn, link.article_id):
                article_count += 1
                continue
            article_html = fetch_html(session, link.article_url, timeout)
            if not article_html:
                continue
            raw_article_path = save_raw(data_dir, date, "articles", f"{link.page_no}-{link.article_no}.html", article_html)
            parsed = parse_article(article_html, link.title)
            txt_path = write_article_txt(data_dir, link, parsed)
            upsert_article(conn, link, parsed, page.page_name, txt_path, raw_article_path)
            article_count += 1
            if sleep_sec:
                time.sleep(sleep_sec)
        conn.commit()
        if sleep_sec:
            time.sleep(sleep_sec)

    conn.commit()
    return len(pages), article_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Crawl Xinhua Daily Telegraph pages/articles into SQLite.")
    parser.add_argument("--start", required=True, help="Start date, YYYYMMDD.")
    parser.add_argument("--end", help="End date, YYYYMMDD. Defaults to --start.")
    parser.add_argument("--db-file", type=Path, default=Path("Spider/XinhuaDaily/xinhua_daily.sqlite3"))
    parser.add_argument("--data-dir", type=Path, default=Path("Spider/XinhuaDaily/data"))
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--force", action="store_true", help="Refetch articles already present in SQLite.")
    parser.add_argument(
        "--no-date-list",
        action="store_true",
        help="Probe every calendar day instead of using mrdx.cn/content/datelist.aspx to skip unpublished dates.",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = build_parser().parse_args()
    start = parse_date(args.start)
    end = parse_date(args.end) if args.end else start
    if start > end:
        raise SystemExit("--start must be <= --end")

    conn = init_db(args.db_file)
    session = requests.Session()
    available_dates = set() if args.no_date_list else fetch_available_dates(session, args.timeout)

    total_pages = 0
    total_articles = 0
    skipped_dates = 0
    for day in date_range(start, end):
        date = day.strftime("%Y%m%d")
        if available_dates and date not in available_dates:
            print(f"[{date}] 不在出版日期列表，跳过")
            skipped_dates += 1
            continue
        pages, articles = crawl_day(conn, session, day, args.data_dir, args.timeout, args.sleep, args.force)
        total_pages += pages
        total_articles += articles

    print(f"done pages={total_pages} articles={total_articles} skipped_dates={skipped_dates} db={args.db_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
