# -*- coding: utf-8 -*-
"""Eastmoney public news crawler.

This crawler intentionally keeps the list-page and article HTML in
source_fetches, because Eastmoney news pages vary by channel.
"""
from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from config import systemConfig


CHANNELS = {
    "finance": "https://finance.eastmoney.com/",
    "stock": "https://stock.eastmoney.com/",
    "kuaixun": "https://kuaixun.eastmoney.com/",
}


@dataclass
class FetchResult:
    url: str
    status_code: int | None
    text: str
    error: str | None = None


def _session():
    s = cffi_requests.Session(impersonate="chrome")
    s.headers.update(systemConfig.HEADERS)
    return s


def _fetch(session, url: str) -> FetchResult:
    try:
        r = session.get(url, timeout=systemConfig.TIMEOUT)
        return FetchResult(url=url, status_code=r.status_code, text=r.text or "")
    except Exception as e:
        return FetchResult(url=url, status_code=None, text="", error=str(e))


def _external_id(url: str) -> str:
    m = re.search(r"/a/([A-Za-z0-9_]+)\.html", url)
    if m:
        return m.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def discover_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    seen = set()
    for a in soup.select("a[href]"):
        href = urljoin(base_url, a.get("href") or "")
        if not href.startswith("http"):
            continue
        if "eastmoney.com" not in href:
            continue
        if not re.search(r"/a/[A-Za-z0-9_]+\.html", href):
            continue
        if href in seen:
            continue
        seen.add(href)
        links.append(href)
    return links


def _text_of(nodes: Iterable) -> str:
    parts = []
    for node in nodes:
        text = node.get_text("\n", strip=True)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def parse_article(url: str, html: str, channel: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = None
    for sel in ("h1", ".title", "#title"):
        n = soup.select_one(sel)
        if n and n.get_text(strip=True):
            title = n.get_text(" ", strip=True)
            break
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True).split("_")[0].strip()

    page_text = soup.get_text("\n", strip=True)
    published_at = None
    m = re.search(r"(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s+\d{1,2}:\d{2}(?::\d{2})?)", page_text)
    if m:
        published_at = (
            m.group(1)
            .replace("年", "-")
            .replace("月", "-")
            .replace("日", "")
            .replace("/", "-")
        )

    author = None
    m = re.search(r"(?:作者|来源)[:：]\s*([^\n]{2,40})", page_text)
    if m:
        author = m.group(1).strip()

    body = None
    selectors = [
        "#ContentBody",
        "#articleContent",
        ".articleContent",
        ".txtinfos",
        ".Body",
        "article",
    ]
    for sel in selectors:
        nodes = soup.select(sel)
        body = _text_of(nodes)
        if body and len(body) > 80:
            break
    if not body:
        ps = [p for p in soup.find_all("p") if len(p.get_text(strip=True)) > 10]
        body = _text_of(ps)

    return {
        "source_name": f"eastmoney_{channel}",
        "external_id": _external_id(url),
        "title": title,
        "author": author,
        "publisher": "东方财富",
        "published_at": published_at,
        "url": url,
        "content_text": body,
        "raw_text": page_text[:200000],
        "topic": channel,
        "metadata": {"channel": channel},
    }


def crawl(channel: str, storage, limit: int = 50) -> int:
    if channel not in CHANNELS:
        raise ValueError(f"unknown news channel: {channel}")
    session = _session()
    source_name = f"eastmoney_{channel}"
    index_url = CHANNELS[channel]
    index = _fetch(session, index_url)
    storage.insert_source_fetch(source_name, f"{channel}:index", index_url, index.status_code, raw_text=index.text, error=index.error)
    if index.error or not index.text:
        print(f"[news] {channel}: 列表页失败 {index.error}")
        return 0

    links = discover_links(index.text, index_url)[:limit]
    total = 0
    for url in links:
        res = _fetch(session, url)
        ext = _external_id(url)
        storage.insert_source_fetch(source_name, ext, url, res.status_code, raw_text=res.text, error=res.error)
        if res.error or res.status_code == 404 or not res.text:
            continue
        article = parse_article(url, res.text, channel)
        if not article.get("title") and not article.get("content_text"):
            continue
        storage.upsert_news_article(article)
        total += 1
    print(f"[news] {channel}: links={len(links)} upsert={total}")
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", choices=sorted(CHANNELS), action="append")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    from eastmoney.pg_storage import PGStorage

    st = PGStorage()
    try:
        channels = args.channel or list(CHANNELS)
        for ch in channels:
            crawl(ch, st, limit=args.limit)
    finally:
        st.close()


if __name__ == "__main__":
    main()
