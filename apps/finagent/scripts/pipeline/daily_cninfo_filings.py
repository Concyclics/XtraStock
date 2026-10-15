#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from psycopg.types.json import Jsonb
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from common import ROOT, connect, today
from v3_ops import record_quality, set_dataset_watermark, stage_run


QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_ROOT = "https://static.cninfo.com.cn/"
CNINFO_PAGE_LIMIT = 100
FINANCIAL_FILING_TYPES = {
    "annual_report", "semiannual_report", "quarterly_report",
    "earnings_flash", "earnings_forecast",
}


def clean_title(value: Any) -> str:
    text = re.sub(r"(?is)<[^>]+>", "", str(value or ""))
    return " ".join(html.unescape(text).split())


def filing_type(title: str) -> str:
    normalized = re.sub(r"\s+", "", title)
    suffix = r"(?:摘要)?(?:[（(]修订版?[）)])?$"
    if re.search(rf"半年度报告{suffix}", normalized):
        return "semiannual_report"
    if re.search(rf"年度报告{suffix}", normalized):
        return "annual_report"
    if re.search(rf"(?:第一|第三)?季度报告{suffix}", normalized):
        return "quarterly_report"
    if re.search(r"业绩快报(?:公告)?$", normalized):
        return "earnings_flash"
    if re.search(r"业绩预告(?:公告)?$", normalized):
        return "earnings_forecast"
    if "权益分派" in title or "分红" in title or re.search(r"利润分配.{0,30}实施(?:的)?公告",title):
        return "corporate_action"
    return "announcement"


def period_end(title: str) -> dt.date | None:
    match = re.search(r"(20\d{2})年(年度|半年度|第一季度|第三季度)", title)
    if not match:
        return None
    year = int(match.group(1))
    return {
        "年度": dt.date(year, 12, 31),
        "半年度": dt.date(year, 6, 30),
        "第一季度": dt.date(year, 3, 31),
        "第三季度": dt.date(year, 9, 30),
    }[match.group(2)]


def exchange_for_stock_code(stock_code: str) -> str | None:
    if re.fullmatch(r"6\d{5}", stock_code):
        return "SSE"
    if re.fullmatch(r"[03]\d{5}", stock_code):
        return "SZSE"
    if re.fullmatch(r"[89]\d{5}", stock_code):
        return "BSE"
    return None


def fetch_page(
    session: requests.Session,
    start: dt.date,
    end: dt.date,
    page: int,
    page_size: int,
    search_key: str = "",
) -> dict[str, Any]:
    response = session.post(
        QUERY_URL,
        data={
            "pageNum": page, "pageSize": page_size, "column": "szse", "tabName": "fulltext",
            "plate": "", "stock": "", "searchkey": search_key, "secid": "", "category": "",
            "trade": "", "seDate": f"{start.isoformat()}~{end.isoformat()}",
            "sortName": "", "sortType": "", "isHLtitle": "true",
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def fetch_range(
    session: requests.Session,
    start: dt.date,
    end: dt.date,
    page_size: int,
    max_pages: int,
    search_key: str = "",
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    first = fetch_page(session, start, end, 1, page_size, search_key)
    total = int(first.get("totalAnnouncement") or 0)
    announcements = list(first.get("announcements") or [])
    if total and not announcements:
        raise RuntimeError('CNINFO returned a nonzero total with an empty first page')
    effective_page_size = len(announcements) or page_size
    required_pages = max(1, (total + effective_page_size - 1) // effective_page_size)
    usable_pages = min(max_pages, CNINFO_PAGE_LIMIT)
    if required_pages > usable_pages and start < end:
        midpoint = start + (end - start) // 2
        left_total, left_rows, left_segments = fetch_range(
            session, start, midpoint, page_size, max_pages, search_key
        )
        right_total, right_rows, right_segments = fetch_range(
            session, midpoint + dt.timedelta(days=1), end, page_size, max_pages, search_key
        )
        return left_total + right_total, left_rows + right_rows, left_segments + right_segments

    pages = min(usable_pages, required_pages)
    seen_ids = {str(row.get('announcementId')) for row in announcements}
    for page in range(2, pages + 1):
        # The endpoint occasionally returns an empty/repeated successful page.
        # Retry it explicitly; an HTTP 200 must not silently truncate history.
        for attempt in range(3):
            payload = fetch_page(session, start, end, page, page_size, search_key)
            batch = payload.get("announcements") or []
            fresh_ids = {str(row.get('announcementId')) for row in batch} - seen_ids
            if fresh_ids:
                break
            time.sleep(0.5 * (attempt + 1))
        if not fresh_ids:
            raise RuntimeError(f'CNINFO page {page}/{required_pages} made no progress for {start}..{end}; got {len(seen_ids)}/{total}')
        announcements.extend(row for row in batch if str(row.get('announcementId')) in fresh_ids)
        seen_ids.update(fresh_ids)
        time.sleep(0.08)
    if pages == required_pages and len(seen_ids) != total:
        raise RuntimeError(f'CNINFO unique announcement count mismatch: {len(seen_ids)}/{total}')
    return total, announcements, [{
        "start": start.isoformat(),
        "end": end.isoformat(),
        "api_total": total,
        "pages": pages,
        "required_pages": required_pages,
        "effective_page_size": effective_page_size,
        "truncated_by_page_limit": pages < required_pages,
    }]


def announced_at(row: dict[str, Any]) -> dt.datetime:
    value = row.get("announcementTime")
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000, dt.timezone(dt.timedelta(hours=8)))
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))


def download_pdf(session: requests.Session, row: dict[str, Any], announce_time: dt.datetime) -> Path | None:
    adjunct = str(row.get("adjunctUrl") or "").lstrip("/")
    if not adjunct or not adjunct.lower().endswith(".pdf"):
        return None
    external_id = str(row.get("announcementId") or Path(adjunct).stem)
    target = ROOT / "filings/cninfo" / str(announce_time.year) / f"{announce_time.month:02d}" / f"{external_id}.pdf"
    if target.exists() and target.stat().st_size > 1000:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    response = session.get(STATIC_ROOT + adjunct, timeout=60)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    if not response.content.startswith(b"%PDF"):
        raise ValueError(f"CNINFO attachment is not PDF: {adjunct}")
    target.write_bytes(response.content)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Incrementally ingest official CNINFO filing metadata.")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=1000)
    parser.add_argument("--search-key", default="")
    parser.add_argument("--corporate-actions-only", action="store_true")
    parser.add_argument("--download-financial-pdfs", action="store_true")
    parser.add_argument("--download-corporate-action-pdfs", action="store_true")
    parser.add_argument(
        "--partition-key",
        default="default",
        help="Watermark partition. Historical backfills must use 'history'.",
    )
    args = parser.parse_args()
    if args.lookback_days < 1:
        parser.error("--lookback-days must be at least 1")
    end = dt.date.fromisoformat(args.end) if args.end else today()
    start = dt.date.fromisoformat(args.start) if args.start else end - dt.timedelta(days=args.lookback_days - 1)
    if start > end:
        parser.error("--start cannot be after --end")

    session = requests.Session()
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        status=6,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; finance-agent-v3/1.0; official filing archive)",
        "Referer": "https://www.cninfo.com.cn/",
        "Origin": "https://www.cninfo.com.cn",
    })
    requested_page_size = args.page_size
    page_size = min(max(1, requested_page_size), 30)
    try:
        total, announcements, segments = fetch_range(
            session, start, end, page_size, args.max_pages, args.search_key.strip()
        )
    except Exception as exc:
        with stage_run(
            "finance_v3_morning", "cninfo_filings", expected_date=end,
            metadata={
                "start": start.isoformat(), "end": end.isoformat(),
                "search_key": args.search_key.strip(), "fetch_phase_failed": True,
                "partition_key": args.partition_key,
            },
        ):
            raise RuntimeError(f"CNINFO fetch failed: {type(exc).__name__}: {exc}") from exc

    raw_unique = {
        str(row.get("announcementId")): row
        for row in announcements if str(row.get("announcementId") or "").strip()
    }
    api_fetched = len(raw_unique)
    announcements = list(raw_unique.values())

    if args.corporate_actions_only:
        announcements = [
            row for row in announcements
            if re.search(
                r"(?:权益分派|利润分配|分红派息).{0,30}实施(?:的)?公告",
                clean_title(row.get("announcementTitle")),
            )
        ]

    inserted = 0
    downloaded = 0
    errors = []
    with stage_run("finance_v3_morning", "cninfo_filings", expected_date=end,
                   metadata={
                       "start": start.isoformat(), "end": end.isoformat(),
                       "partition_key": args.partition_key,
                   }) as run:
        with connect(admin=False) as conn:
            for raw in announcements:
                try:
                    title = clean_title(raw.get("announcementTitle"))
                    external_id = str(raw.get("announcementId") or "").strip()
                    stock_code = str(raw.get("secCode") or "").strip() or None
                    if not external_id or not title:
                        continue
                    at = announced_at(raw)
                    adjunct = str(raw.get("adjunctUrl") or "").lstrip("/")
                    url = STATIC_ROOT + adjunct if adjunct else None
                    asset_row = conn.execute(
                        "SELECT asset_id FROM market.assets WHERE symbol=%s ORDER BY updated_at DESC LIMIT 1",
                        (stock_code,),
                    ).fetchone() if stock_code else None
                    if stock_code and not asset_row and exchange_for_stock_code(stock_code):
                        conn.execute(
                            """
                            INSERT INTO market.assets (
                              asset_type,symbol,exchange,name,metadata,created_at,updated_at
                            ) VALUES ('stock',%s,%s,%s,%s,now(),now())
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                stock_code, exchange_for_stock_code(stock_code), clean_title(raw.get("secName") or ""),
                                Jsonb({
                                    "source": "cninfo_official_filing",
                                    "official_placeholder": True,
                                    "is_active": True,
                                }),
                            ),
                        )
                        asset_row = conn.execute(
                            "SELECT asset_id FROM market.assets WHERE symbol=%s ORDER BY updated_at DESC LIMIT 1",
                            (stock_code,),
                        ).fetchone()
                    asset_id = asset_row["asset_id"] if asset_row else None
                    document = conn.execute(
                        """
                        INSERT INTO corpus.documents (
                          source_type,source_name,external_id,title,publisher,published_at,url,
                          metadata,ingest_status,created_at,updated_at
                        ) VALUES ('announcement','cninfo',%s,%s,'巨潮资讯网',%s,%s,%s,'ok',now(),now())
                        ON CONFLICT (source_type,source_name,external_id) DO UPDATE SET
                          title=EXCLUDED.title,published_at=EXCLUDED.published_at,url=EXCLUDED.url,
                          metadata=corpus.documents.metadata||EXCLUDED.metadata,
                          ingest_status='ok',updated_at=now()
                        WHERE (corpus.documents.title,corpus.documents.published_at,corpus.documents.url,corpus.documents.metadata)
                          IS DISTINCT FROM (EXCLUDED.title,EXCLUDED.published_at,EXCLUDED.url,corpus.documents.metadata||EXCLUDED.metadata)
                        RETURNING id
                        """,
                        (external_id,title,at,url,Jsonb({
                            "stock_code": stock_code, "stock_name": clean_title(raw.get("secName") or ""),
                            "category": raw.get("announcementTypeName"), "adjunct_url": adjunct,
                            "official_source": True,
                        })),
                    ).fetchone()
                    document_id = document["id"] if document else conn.execute(
                        "SELECT id FROM corpus.documents WHERE source_type='announcement' AND source_name='cninfo' AND external_id=%s",(external_id,)).fetchone()['id']
                    kind = filing_type(title)
                    conn.execute(
                        """
                        INSERT INTO company.filings (
                          asset_id,stock_code,filing_type,title,period_end,announced_at,available_at,
                          source_name,source_external_id,source_url,document_id,revision_no,
                          quality_status,metadata,updated_at
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,'cninfo',%s,%s,%s,1,'verified',%s,now())
                        ON CONFLICT (source_name,source_external_id,revision_no) DO UPDATE SET
                          asset_id=EXCLUDED.asset_id,stock_code=EXCLUDED.stock_code,
                          filing_type=EXCLUDED.filing_type,title=EXCLUDED.title,
                          period_end=EXCLUDED.period_end,announced_at=EXCLUDED.announced_at,
                          available_at=EXCLUDED.available_at,source_url=EXCLUDED.source_url,
                          document_id=EXCLUDED.document_id,quality_status=EXCLUDED.quality_status,
                          metadata=company.filings.metadata||EXCLUDED.metadata,updated_at=now()
                        """,
                        (asset_id,stock_code,kind,title,period_end(title),at,at,external_id,url,document_id,
                         Jsonb({"stock_name": clean_title(raw.get("secName") or ""), "official_source": True})),
                    )
                    should_download = (
                        (args.download_financial_pdfs and kind in FINANCIAL_FILING_TYPES)
                        or (
                            args.download_corporate_action_pdfs
                            and re.search(r"(?:权益分派|利润分配|分红派息).{0,30}实施(?:的)?公告", title)
                        )
                    )
                    if should_download:
                        path = download_pdf(session, raw, at)
                        if path:
                            downloaded += 1
                            conn.execute(
                                """
                                INSERT INTO corpus.document_files (
                                  document_id,file_role,file_path,mime_type,size_bytes,ocr_status
                                ) VALUES (%s,'pdf',%s,'application/pdf',%s,'pending')
                                ON CONFLICT (document_id,file_role,file_path) DO UPDATE SET
                                  size_bytes=EXCLUDED.size_bytes,ocr_status=CASE
                                    WHEN corpus.document_files.ocr_status='ok' THEN 'ok' ELSE 'pending' END
                                """,
                                (document_id,str(path),path.stat().st_size),
                            )
                    inserted += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append({"external_id": raw.get("announcementId"), "error": f"{type(exc).__name__}: {exc}"[:400]})

        coverage = 100.0 * api_fetched / total if total else 100.0
        quality = "pass" if coverage >= 99 and not errors else (
            "fail" if coverage < 99 else "warn"
        )
        record_quality(
            run["run_id"], "company.filings", "cninfo_api_coverage",
            status="pass" if coverage >= 99 else "fail", severity="error",
            partition_key=args.partition_key,
            observed_value=coverage,threshold_value=99,affected_rows=max(total-api_fetched,0),
            sample=errors[:20],details={
                "api_total": total,
                "segments": segments,
                "truncated_by_page_limit": any(
                    segment["truncated_by_page_limit"] for segment in segments
                ),
                "requested_page_size": requested_page_size,
                "api_page_size": page_size,
            },
        )
        set_dataset_watermark(
            "company.filings",schema_version="v3",run_id=run["run_id"],expected_date=end,
            partition_key=args.partition_key,
            data_through=dt.datetime.combine(end,dt.time(23,59),dt.timezone(dt.timedelta(hours=8))),
            row_count=inserted,eligible_count=len(announcements),coverage_pct=min(coverage,100),quality_status=quality,
            source_version="cninfo:hisAnnouncement:v1",
            details={"start": start.isoformat(), "end": end.isoformat(), "downloaded_pdfs": downloaded, "errors": errors[:20]},
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {
            f"company.filings:{args.partition_key}": end.isoformat()
        }
    print({
        "api_total": total, "api_fetched": api_fetched,
        "filtered": len(announcements), "upserted": inserted,
        "downloaded_pdfs": downloaded, "errors": len(errors),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
