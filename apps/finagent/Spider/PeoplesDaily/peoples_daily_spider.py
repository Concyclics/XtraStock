#!/usr/bin/env python3
"""Unified People's Daily crawler and incremental CSV updater."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import bs4
import requests


BASE_OLD = "http://paper.people.com.cn/rmrb/html/"
BASE_NEW = "http://paper.people.com.cn/rmrb/pc/"
NEW_SITE_START = dt.date(2024, 12, 4)
CSV_FIELDS = ["id", "date", "title", "content"]
TXT_RE = re.compile(r"^(\d{8})-\d{2}-\d{2}\.txt$")
OLD_ARTICLE_RE = re.compile(r"nw\.D110000renmrb_(?:\d{8}_)?(?P<article>\d+)-(?P<page>\d+)\.htm")
OLD_PAGE_RE = re.compile(r"(?:nbs\.)?D110000renmrb_(?P<page>\d+)\.htm")
NEW_PAGE_RE = re.compile(r"node_(?P<page>\d+)\.html")
PDF_RE = re.compile(r"\.pdf(?:$|\?)", re.I)
IMAGE_RE = re.compile(r"\.(?:jpg|jpeg|png)(?:[.\w-]*)?(?:$|\?)", re.I)
csv.field_size_limit(sys.maxsize)

SQL_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS issues (
    date TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    issue_url TEXT NOT NULL,
    site_version TEXT NOT NULL,
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
    page_name TEXT,
    article_url TEXT NOT NULL,
    content_text TEXT,
    txt_path TEXT,
    raw_html_path TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE(date, page_no, article_no)
);

CREATE INDEX IF NOT EXISTS idx_peoples_daily_pages_date ON pages(date);
CREATE INDEX IF NOT EXISTS idx_peoples_daily_articles_date ON articles(date);
CREATE INDEX IF NOT EXISTS idx_peoples_daily_articles_page ON articles(date, page_no);
"""


@dataclass(frozen=True)
class PageMeta:
    url: str
    page_no: str
    page_name: str = ""

    @property
    def page_id(self) -> str:
        return self.page_no


@dataclass(frozen=True)
class Article:
    url: str
    page_no: str
    article_no: str
    title: str = ""
    page_name: str = ""
    sort_order: int = 0

    @property
    def article_id(self) -> str:
        return f"{self.page_no}-{self.article_no}"


@dataclass(frozen=True)
class IssuePages:
    issue_url: str
    site_version: str
    pages: list[PageMeta]


def normalize_text(text: str) -> str:
    """Fix common mojibake from the paper site while leaving normal text intact."""
    if not text:
        return ""
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except UnicodeEncodeError:
        return text
    except UnicodeDecodeError:
        return text
    return fixed


def clean_text(text: str) -> str:
    text = normalize_text(text or "")
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def init_db(db_file: Path) -> sqlite3.Connection:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.executescript(SQL_SCHEMA)
    return conn


def save_raw_html(data_dir: Path, day_prefix: str, kind: str, name: str, html: str) -> Path:
    raw_dir = data_dir / "raw" / day_prefix / kind
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / name
    path.write_text(html, encoding="utf-8")
    return path


def parse_page_label(text: str, fallback_no: str) -> tuple[str, str]:
    text = clean_text(text)
    if not text:
        return fallback_no, ""
    match = re.search(r"(?P<page>\d{1,2})\s*(?:版|:|：)\s*[:：]?\s*(?P<name>.+)", text)
    if not match:
        return fallback_no, text
    return match.group("page").zfill(2), clean_text(match.group("name"))


def first_asset_url(soup: bs4.BeautifulSoup, page_url: str, pattern: re.Pattern[str]) -> str:
    for link in soup.find_all(["a", "img"]):
        href = link.get("href") or link.get("src") or ""
        if href and pattern.search(href):
            return urljoin(page_url, href)
    return ""


def fetch_html(session: requests.Session, url: str, timeout: int = 20) -> str | None:
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
    }
    try:
        response = session.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"访问失败: {url} ({exc})")
        return None
    head = response.content[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", head, re.I)
    if match:
        encoding = match.group(1)
    else:
        encoding = response.encoding or response.apparent_encoding or "utf-8"
    return response.content.decode(encoding, errors="replace")


def date_range(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)


def parse_yyyymmdd(value: str) -> dt.date:
    return dt.datetime.strptime(value, "%Y%m%d").date()


def find_latest_txt_date(data_dir: Path) -> dt.date | None:
    latest: dt.date | None = None
    if not data_dir.exists():
        return None
    for path in data_dir.iterdir():
        if not path.is_file():
            continue
        match = TXT_RE.match(path.name)
        if not match:
            continue
        day = parse_yyyymmdd(match.group(1))
        if latest is None or day > latest:
            latest = day
    return latest


def resolve_crawl_dates(data_dir: Path, start: str | None, end: str | None) -> tuple[dt.date, dt.date]:
    end_date = parse_yyyymmdd(end) if end else dt.date.today()
    if start:
        start_date = parse_yyyymmdd(start)
    else:
        latest = find_latest_txt_date(data_dir)
        if latest is None:
            raise SystemExit("data 目录中没有历史 txt，请用 --start YYYYMMDD 指定起始日期。")
        start_date = latest + dt.timedelta(days=1)

    if start_date > end_date:
        print(f"无需更新：起始日期 {start_date:%Y%m%d} 晚于结束日期 {end_date:%Y%m%d}")
    return start_date, end_date


def old_issue_pages(session: requests.Session, day: dt.date) -> IssuePages:
    ymd = day.strftime("%Y-%m/%d")
    url = f"{BASE_OLD}{ymd}/nbs.D110000renmrb_01.htm"
    html = fetch_html(session, url)
    if not html:
        return IssuePages(url, "old", [])

    soup = bs4.BeautifulSoup(html, "html.parser")
    page_box = soup.find("div", attrs={"id": "pageList"})
    if page_box:
        pages = page_box.ul.find_all("div", attrs={"class": "right_title-name"})
    else:
        swiper = soup.find("div", attrs={"class": "swiper-container"})
        pages = swiper.find_all("div", attrs={"class": "swiper-slide"}) if swiper else []

    result: list[PageMeta] = []
    for page in pages:
        link = page.find("a")
        if not link:
            continue
        href = link.get("href", "")
        match = OLD_PAGE_RE.search(href)
        page_no = match.group("page").zfill(2) if match else f"{len(result) + 1:02d}"
        label = page.get_text(" ", strip=True)
        page_no, page_name = parse_page_label(label, page_no)
        result.append(PageMeta(urljoin(url, href), page_no, page_name))
    return IssuePages(url, "old", result)


def old_page_urls(session: requests.Session, day: dt.date) -> list[str]:
    return [page.url for page in old_issue_pages(session, day).pages]


def old_page_articles(page: PageMeta, html: str) -> list[Article]:
    articles: list[Article] = []
    soup = bs4.BeautifulSoup(html, "html.parser")
    news_list = soup.find("ul", attrs={"class": "news-list"})
    if news_list:
        title_items = news_list.find_all("li")
    else:
        title_box = soup.find("div", attrs={"id": "titleList"})
        title_items = title_box.ul.find_all("li") if title_box and title_box.ul else []

    sort_order = 1
    for item in title_items:
        for link in item.find_all("a"):
            href = link.get("href", "")
            match = OLD_ARTICLE_RE.search(href)
            if not match:
                continue
            article_url = urljoin(page.url, href)
            page_no = match.group("page").zfill(2)
            article_no = match.group("article").zfill(2)
            articles.append(
                Article(
                    article_url,
                    page_no,
                    article_no,
                    clean_text(link.get_text(" ", strip=True)),
                    page.page_name,
                    sort_order,
                )
            )
            sort_order += 1
    return articles


def old_articles(session: requests.Session, day: dt.date) -> list[Article]:
    articles: list[Article] = []
    for page in old_issue_pages(session, day).pages:
        html = fetch_html(session, page.url)
        if html:
            articles.extend(old_page_articles(page, html))
    return articles


def old_article_text(session: requests.Session, url: str) -> str | None:
    html = fetch_html(session, url)
    if not html:
        return None
    return old_article_text_from_html(html)


def old_article_text_from_html(html: str) -> str | None:
    soup = bs4.BeautifulSoup(html, "html.parser")
    title_parts = []
    for tag_name in ("h3", "h1", "h2"):
        tag = soup.find(tag_name)
        title_parts.append(tag.get_text(strip=True) if tag else "")
    body = soup.find("div", attrs={"id": "ozoom"})
    paragraphs = [p.get_text(strip=True) for p in body.find_all("p")] if body else []
    return "\n".join(title_parts + paragraphs).strip() + "\n"


def new_issue_pages(session: requests.Session, day: dt.date) -> IssuePages:
    url = f"{BASE_NEW}layout/{day:%Y%m}/{day:%d}/node_01.html"
    html = fetch_html(session, url)
    if not html:
        return IssuePages(url, "new", [])

    soup = bs4.BeautifulSoup(html, "html.parser")
    result: list[PageMeta] = []
    for link in soup.find_all("a", id="pageLink"):
        href = link.get("href")
        if not href:
            continue
        text = normalize_text(link.get_text(" ", strip=True))
        if "广告" in text:
            continue
        match = NEW_PAGE_RE.search(href)
        page_no = match.group("page").zfill(2) if match else f"{len(result) + 1:02d}"
        page_no, page_name = parse_page_label(text, page_no)
        result.append(PageMeta(urljoin(url, href), page_no, page_name))
    return IssuePages(url, "new", result)


def new_page_urls(session: requests.Session, day: dt.date) -> list[tuple[str, str]]:
    return [(page.url, page.page_no) for page in new_issue_pages(session, day).pages]


def new_page_articles(page: PageMeta, html: str) -> list[Article]:
    articles: list[Article] = []
    soup = bs4.BeautifulSoup(html, "html.parser")
    article_no = 1
    for news_list in soup.find_all("ul", class_="news-list"):
        for item in news_list.find_all("li"):
            link = item.find("a")
            href = link.get("href") if link else None
            if not href:
                continue
            articles.append(
                Article(
                    urljoin(page.url, href),
                    page.page_no,
                    f"{article_no:02d}",
                    clean_text(link.get_text(" ", strip=True)),
                    page.page_name,
                    article_no,
                )
            )
            article_no += 1
    return articles


def new_articles(session: requests.Session, day: dt.date) -> list[Article]:
    articles: list[Article] = []
    for page in new_issue_pages(session, day).pages:
        html = fetch_html(session, page.url)
        if html:
            articles.extend(new_page_articles(page, html))
    return articles


def new_article_text(session: requests.Session, url: str) -> str | None:
    html = fetch_html(session, url)
    if not html:
        return None
    return new_article_text_from_html(html)


def new_article_text_from_html(html: str) -> str | None:
    soup = bs4.BeautifulSoup(html, "html.parser")
    article = soup.find("div", class_="article")
    if not article:
        return None
    text = normalize_text(article.get_text("\n", strip=False))
    return text.strip() + "\n"


def get_articles(session: requests.Session, day: dt.date) -> list[Article]:
    if day >= NEW_SITE_START:
        return new_articles(session, day)
    return old_articles(session, day)


def get_issue_pages(session: requests.Session, day: dt.date) -> IssuePages:
    if day >= NEW_SITE_START:
        return new_issue_pages(session, day)
    return old_issue_pages(session, day)


def get_article_text(session: requests.Session, day: dt.date, url: str) -> str | None:
    if day >= NEW_SITE_START:
        return new_article_text(session, url)
    return old_article_text(session, url)


def article_text_from_html(day: dt.date, html: str) -> str | None:
    if day >= NEW_SITE_START:
        return new_article_text_from_html(html)
    return old_article_text_from_html(html)


def save_txt(data_dir: Path, article_id: str, content: str) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f"{article_id}.txt"
    path.write_text(content, encoding="utf-8")
    return path


def page_assets(page: PageMeta, html: str) -> tuple[str, str]:
    soup = bs4.BeautifulSoup(html, "html.parser")
    pdf_url = first_asset_url(soup, page.url, PDF_RE)
    image_url = ""
    for image in soup.find_all("img"):
        src = image.get("src", "")
        if not src or not IMAGE_RE.search(src):
            continue
        lowered = src.lower()
        if any(skip in lowered for skip in ("logo", "icon", "ios", "android", "mini", "/files/")):
            continue
        if "pic/" in lowered or image.get("usemap") or image.get("USEMAP"):
            image_url = urljoin(page.url, src)
            break
    if not image_url:
        image_url = first_asset_url(soup, page.url, IMAGE_RE)
    return pdf_url, image_url


def upsert_issue(conn: sqlite3.Connection, date: str, issue: IssuePages) -> None:
    conn.execute(
        """
        INSERT INTO issues(date, source, issue_url, site_version, page_count, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            issue_url=excluded.issue_url,
            site_version=excluded.site_version,
            page_count=excluded.page_count,
            fetched_at=excluded.fetched_at
        """,
        (date, "人民日报", issue.issue_url, issue.site_version, len(issue.pages), now_iso()),
    )


def upsert_page(
    conn: sqlite3.Connection,
    date: str,
    page: PageMeta,
    pdf_url: str,
    image_url: str,
    raw_html_path: Path,
) -> None:
    page_id = f"{date}-{page.page_no}"
    conn.execute(
        """
        INSERT INTO pages(page_id, date, page_no, page_name, page_url, pdf_url, image_url, raw_html_path, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            page_name=excluded.page_name,
            page_url=excluded.page_url,
            pdf_url=excluded.pdf_url,
            image_url=excluded.image_url,
            raw_html_path=excluded.raw_html_path,
            fetched_at=excluded.fetched_at
        """,
        (page_id, date, page.page_no, page.page_name, page.url, pdf_url, image_url, str(raw_html_path), now_iso()),
    )


def upsert_article(
    conn: sqlite3.Connection,
    date: str,
    article: Article,
    content: str,
    txt_path: Path,
    raw_html_path: Path | None,
) -> None:
    article_id = f"{date}-{article.article_id}"
    parsed_txt = row_from_txt(txt_path) if txt_path.exists() else None
    title = article.title or (parsed_txt["title"] if parsed_txt else "")
    conn.execute(
        """
        INSERT INTO articles(
            article_id, date, page_no, article_no, sort_order, title, page_name,
            article_url, content_text, txt_path, raw_html_path, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_id) DO UPDATE SET
            sort_order=excluded.sort_order,
            title=excluded.title,
            page_name=excluded.page_name,
            article_url=excluded.article_url,
            content_text=excluded.content_text,
            txt_path=excluded.txt_path,
            raw_html_path=COALESCE(NULLIF(excluded.raw_html_path, ''), articles.raw_html_path),
            fetched_at=excluded.fetched_at
        """,
        (
            article_id,
            date,
            article.page_no,
            article.article_no,
            article.sort_order,
            title,
            article.page_name,
            article.url,
            content,
            str(txt_path),
            str(raw_html_path) if raw_html_path else "",
            now_iso(),
        ),
    )


def load_existing_csv_ids(csv_path: Path) -> set[str]:
    if not csv_path.exists():
        return set()
    ids: set[str] = set()
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            article_id = row.get("id")
            if article_id:
                ids.add(article_id)
    return ids


def row_from_txt(path: Path) -> dict[str, str] | None:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    text = "\n".join(lines)
    if len(lines) < 8 and "广告" in text:
        return None

    title_parts: list[str] = []
    for i in range(min(3, len(lines))):
        current = lines[i].strip()
        previous = lines[i - 1].strip() if i > 0 else None
        if current and current != previous:
            title_parts.append(current)

    title = " ".join(title_parts).strip()
    if (
        not title
        or "责编" in title
        or "版式设计" in title
        or "广告" in title
        or "图片报道" in title
    ):
        return None

    content = "\n".join(line for line in lines[3:] if line.strip())
    if not content.strip():
        return None

    article_id = path.stem
    return {
        "id": article_id,
        "date": article_id[:8],
        "title": title,
        "content": content,
    }


def append_rows(csv_path: Path, rows: list[dict[str, str]]) -> int:
    if not rows:
        return 0
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def append_txts_to_csv(
    clean_dir: Path,
    txt_paths: Iterable[Path],
    existing_ids_by_year: dict[str, set[str]] | None = None,
) -> int:
    rows_by_year: dict[str, list[dict[str, str]]] = {}
    if existing_ids_by_year is None:
        existing_ids_by_year = {}

    for path in sorted(txt_paths):
        match = TXT_RE.match(path.name)
        if not match:
            continue
        year = match.group(1)[:4]
        csv_path = clean_dir / f"RenMin_Daily_{year}.csv"
        if year not in existing_ids_by_year:
            existing_ids_by_year[year] = load_existing_csv_ids(csv_path)
        if path.stem in existing_ids_by_year[year]:
            continue
        row = row_from_txt(path)
        if row is None:
            continue
        rows_by_year.setdefault(year, []).append(row)
        existing_ids_by_year[year].add(path.stem)

    appended = 0
    for year, rows in rows_by_year.items():
        appended += append_rows(clean_dir / f"RenMin_Daily_{year}.csv", rows)
    return appended


def sync_csv_from_existing_txt(data_dir: Path, clean_dir: Path, year: str | None = None) -> int:
    if year:
        txts = data_dir.glob(f"{year}*.txt")
    else:
        txts = data_dir.glob("*.txt")
    return append_txts_to_csv(clean_dir, txts)


def crawl(
    data_dir: Path,
    clean_dir: Path,
    start: dt.date,
    end: dt.date,
    sleep_seconds: float,
    db_file: Path | None,
) -> None:
    if start > end:
        return

    total_saved = 0
    total_csv = 0
    existing_ids_by_year: dict[str, set[str]] = {}
    conn = init_db(db_file) if db_file else None
    with requests.Session() as session:
        try:
            for day in date_range(start, end):
                day_prefix = day.strftime("%Y%m%d")
                issue = get_issue_pages(session, day)
                if conn:
                    upsert_issue(conn, day_prefix, issue)
                    issue_html = fetch_html(session, issue.issue_url)
                    if issue_html:
                        save_raw_html(data_dir, day_prefix, "issue", "issue.html", issue_html)

                saved_paths: list[Path] = []
                skipped = 0
                article_total = 0
                for page in issue.pages:
                    page_html = fetch_html(session, page.url)
                    if not page_html:
                        continue
                    if conn:
                        raw_page_path = save_raw_html(data_dir, day_prefix, "pages", f"{page.page_no}.html", page_html)
                        pdf_url, image_url = page_assets(page, page_html)
                        upsert_page(conn, day_prefix, page, pdf_url, image_url, raw_page_path)
                    else:
                        raw_page_path = None

                    if day >= NEW_SITE_START:
                        articles = new_page_articles(page, page_html)
                    else:
                        articles = old_page_articles(page, page_html)
                    article_total += len(articles)

                    for article in articles:
                        article_id = f"{day_prefix}-{article.article_id}"
                        txt_path = data_dir / f"{article_id}.txt"
                        raw_article_path: Path | None = None
                        if txt_path.exists():
                            skipped += 1
                            content = txt_path.read_text(encoding="utf-8", errors="ignore")
                            saved_paths.append(txt_path)
                        else:
                            article_html = fetch_html(session, article.url)
                            if not article_html:
                                continue
                            if conn:
                                raw_article_path = save_raw_html(
                                    data_dir,
                                    day_prefix,
                                    "articles",
                                    f"{article.page_no}-{article.article_no}.html",
                                    article_html,
                                )
                            content = article_text_from_html(day, article_html)
                            if not content:
                                continue
                            txt_path = save_txt(data_dir, article_id, content)
                            saved_paths.append(txt_path)
                            total_saved += 1
                            if sleep_seconds:
                                time.sleep(sleep_seconds)

                        if conn:
                            upsert_article(conn, day_prefix, article, content, txt_path, raw_article_path)

                    if conn:
                        conn.commit()

                appended = append_txts_to_csv(clean_dir, saved_paths, existing_ids_by_year)
                total_csv += appended
                print(
                    f"{day_prefix}: 版面 {len(issue.pages)} 个，发现 {article_total} 篇，"
                    f"新增 txt {len(saved_paths) - skipped} 篇，CSV 追加 {appended} 行"
                )
        finally:
            if conn:
                conn.commit()
                conn.close()

    print(f"完成：新增 txt {total_saved} 篇，CSV 追加 {total_csv} 行")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="人民日报统一爬虫，自动续爬并增量更新 CSV，同时保留期次/版面/文章 SQLite 数据。")
    parser.add_argument("--data-dir", default="data", help="txt 输出目录，默认 data")
    parser.add_argument("--clean-dir", default="clean_data", help="CSV 输出目录，默认 clean_data")
    parser.add_argument(
        "--db-file",
        default="peoples_daily.sqlite3",
        help="SQLite 输出文件；传空字符串可关闭 SQLite 入库，默认 peoples_daily.sqlite3",
    )
    parser.add_argument("--start", help="起始日期 YYYYMMDD；不传则从 data 中最新日期的下一天开始")
    parser.add_argument("--end", help="结束日期 YYYYMMDD；不传则为今天")
    parser.add_argument("--sleep", type=float, default=0.0, help="每篇文章下载后的等待秒数")
    parser.add_argument(
        "--sync-csv",
        action="store_true",
        help="不爬取，只把现有 txt 中缺失的记录追加到年份 CSV",
    )
    parser.add_argument("--year", help="配合 --sync-csv 使用，只同步某一年")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    clean_dir = Path(args.clean_dir)
    db_file = Path(args.db_file) if args.db_file else None

    if args.sync_csv:
        appended = sync_csv_from_existing_txt(data_dir, clean_dir, args.year)
        print(f"CSV 同步完成：追加 {appended} 行")
        return

    start, end = resolve_crawl_dates(data_dir, args.start, args.end)
    print(f"更新区间：{start:%Y%m%d} 至 {end:%Y%m%d}")
    crawl(data_dir, clean_dir, start, end, args.sleep, db_file)


if __name__ == "__main__":
    main()
