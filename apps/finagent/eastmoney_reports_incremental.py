from __future__ import annotations

import argparse
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import lxml.html
import pandas as pd
import requests
from tqdm import tqdm


HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Referer": "https://data.eastmoney.com/report/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
}

REPORT_API = "https://reportapi.eastmoney.com/report/list"
DEFAULT_DB_FILE = "eastmoney_reports.sqlite3"

STOCK_COLUMNS = [
    "股票代码",
    "股票简称",
    "东财评级",
    "评级变动",
    "机构",
    "近一月个股研报数",
    "2024盈利预测",
    "2024市盈率预测",
    "2025盈利预测",
    "2025市盈率预测",
    "预测当年",
    "预测年份口径",
    "当年盈利预测",
    "当年市盈率预测",
    "次年盈利预测",
    "次年市盈率预测",
    "后年盈利预测",
    "后年市盈率预测",
    "行业",
    "日期",
    "报告名称",
    "报告链接",
    "原始文本",
]

INDUSTRY_COLUMNS = [
    "行业",
    "涨跌幅",
    "东财评级",
    "评级变动",
    "机构",
    "近一月行业研报数",
    "日期",
    "报告名称",
    "报告链接",
    "原始文本",
]

INVALID_FILENAME_CHARS = r'\/:*?"<>|'
INTERNAL_COLUMNS = ["详情页状态", "详情页状态时间"]


class NotFoundError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReportKind:
    name: str
    q_type: int
    list_file: str
    detail_file: str
    checkpoint_prefix: str
    pdf_dir: str
    columns: list[str]
    detail_page: str


KINDS = {
    "stock": ReportKind(
        name="stock",
        q_type=0,
        list_file="东财研报.xlsx",
        detail_file="东财研报详细.xlsx",
        checkpoint_prefix="东财研报详细",
        pdf_dir="PDF",
        columns=STOCK_COLUMNS,
        detail_page="zw_stock.jshtml",
    ),
    "industry": ReportKind(
        name="industry",
        q_type=1,
        list_file="行业研报.xlsx",
        detail_file="行业研报详细.xlsx",
        checkpoint_prefix="行业研报详细",
        pdf_dir="PDF_ind",
        columns=INDUSTRY_COLUMNS,
        detail_page="zw_industry.jshtml",
    ),
}


def parse_args(argv: list[str] | None = None, default_kinds: Iterable[str] = ("stock", "industry")):
    parser = argparse.ArgumentParser(description="东方财富研报增量更新")
    parser.add_argument("--kinds", nargs="+", choices=sorted(KINDS), default=list(default_kinds))
    parser.add_argument("--start-date", help="目录更新起始日期，格式 YYYY-MM-DD；默认使用现有最大日期向前回看")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="目录更新结束日期，默认今天")
    parser.add_argument("--lookback-days", type=int, default=3, help="从现有最大日期向前回看的天数")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--chunk-days", type=int, default=31, help="按日期分段请求，避免单次分页过大")
    parser.add_argument("--sleep", type=float, default=0.25, help="请求间隔秒数")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--checkpoint-every", type=int, default=50)
    parser.add_argument("--storage", choices=["sqlite", "excel"], default="sqlite", help="主存储方式；sqlite 支持逐条增量写入")
    parser.add_argument("--db-file", default=DEFAULT_DB_FILE, help="SQLite 数据库文件")
    parser.add_argument("--export-excel", action="store_true", help="sqlite 模式结束后额外导出 Excel")
    parser.add_argument("--limit-list-pages", type=int, default=0, help="调试用：每个日期段最多抓取页数，0 表示不限")
    parser.add_argument("--limit-details", type=int, default=0, help="调试用：最多补正文/PDF条数，0 表示不限")
    parser.add_argument("--list-only", action="store_true", help="只更新目录，不补正文/PDF")
    parser.add_argument("--no-pdf", action="store_true", help="补正文但不下载 PDF")
    parser.add_argument("--dry-run", action="store_true", help="只检查抓取和合并结果，不写 Excel/PDF")
    return parser.parse_args(argv)


def request_with_retry(session: requests.Session, url: str, *, retries: int, **kwargs) -> requests.Response:
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            response = session.get(url, timeout=30, **kwargs)
            if response.status_code == 404:
                raise NotFoundError(f"404 not found: {url}")
            response.raise_for_status()
            return response
        except NotFoundError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(min(8, attempt * 1.5) + random.random())
    raise RuntimeError(f"request failed after {retries} attempts: {url}") from last_exc


def read_excel_safely(path: Path) -> pd.DataFrame | None:
    try:
        df = pd.read_excel(path, engine="openpyxl")
    except Exception as exc:
        print(f"[skip] {path.name}: {type(exc).__name__}: {exc}")
        return None
    return df.drop(columns=[c for c in df.columns if str(c).startswith("Unnamed:")], errors="ignore")


def existing_candidates(kind: ReportKind) -> list[Path]:
    names = [
        kind.detail_file,
        f"{kind.checkpoint_prefix}_0.xlsx",
        f"{kind.checkpoint_prefix}_1.xlsx",
        f"{kind.checkpoint_prefix}_2.xlsx",
        kind.list_file,
    ]
    return [Path(name) for name in names if Path(name).exists()]


def load_existing(kind: ReportKind) -> pd.DataFrame:
    frames = []
    for path in existing_candidates(kind):
        df = read_excel_safely(path)
        if df is not None:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=kind.columns)
    merged = pd.concat(frames, ignore_index=True)
    for col in kind.columns:
        if col not in merged.columns:
            merged[col] = pd.NA
    return dedupe_reports(merged[kind.columns])


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sqlite_table(kind: ReportKind) -> str:
    return f"{kind.name}_reports"


def sqlite_value(value):
    if pd.isna(value):
        return None
    text = str(value)
    return None if text.lower() == "nan" else text


def connect_sqlite(db_file: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_sqlite_table(conn: sqlite3.Connection, kind: ReportKind):
    table = quote_ident(sqlite_table(kind))
    cols = ", ".join(f"{quote_ident(col)} TEXT" for col in kind.columns)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols}, PRIMARY KEY ({quote_ident('报告链接')}))")
    conn.execute(f"CREATE INDEX IF NOT EXISTS {quote_ident(sqlite_table(kind) + '_date_idx')} ON {table} ({quote_ident('日期')})")
    existing_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col in list(kind.columns) + INTERNAL_COLUMNS:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {quote_ident(col)} TEXT")
    conn.commit()


def upsert_reports_sqlite(conn: sqlite3.Connection, kind: ReportKind, df: pd.DataFrame):
    if df.empty:
        return
    ensure_sqlite_table(conn, kind)
    table = quote_ident(sqlite_table(kind))
    columns = list(kind.columns) + [col for col in INTERNAL_COLUMNS if col in df.columns]
    placeholders = ", ".join("?" for _ in columns)
    insert_cols = ", ".join(quote_ident(col) for col in columns)
    updates = ", ".join(
        f"{quote_ident(col)} = CASE "
        f"WHEN excluded.{quote_ident(col)} IS NOT NULL AND excluded.{quote_ident(col)} != '' "
        f"THEN excluded.{quote_ident(col)} ELSE {quote_ident(col)} END"
        for col in columns
        if col != "报告链接"
    )
    sql = (
        f"INSERT INTO {table} ({insert_cols}) VALUES ({placeholders}) "
        f"ON CONFLICT({quote_ident('报告链接')}) DO UPDATE SET {updates}"
    )
    rows = [tuple(sqlite_value(row.get(col)) for col in columns) for _, row in df.iterrows()]
    with conn:
        conn.executemany(sql, rows)


def update_report_sqlite(conn: sqlite3.Connection, kind: ReportKind, row: pd.Series):
    columns = list(kind.columns) + [col for col in INTERNAL_COLUMNS if col in row.index]
    upsert_reports_sqlite(conn, kind, pd.DataFrame([row], columns=columns))


def load_existing_sqlite(conn: sqlite3.Connection, kind: ReportKind, db_file: str) -> pd.DataFrame:
    ensure_sqlite_table(conn, kind)
    table = quote_ident(sqlite_table(kind))
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if count == 0:
        existing = load_existing(kind)
        if not existing.empty:
            print(f"[{kind.name}] importing existing Excel/checkpoints into {db_file}: {len(existing)} rows")
            upsert_reports_sqlite(conn, kind, existing)
        return existing
    df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
    columns = list(kind.columns) + INTERNAL_COLUMNS
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return dedupe_reports(df[columns])


def report_link(kind: ReportKind, info_code: str) -> str:
    return f"https://data.eastmoney.com/report/{kind.detail_page}?infocode={info_code}"


def clean_date(value) -> str:
    if not value:
        return ""
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return str(value)[:10]
    return parsed.date().isoformat()


def rating_change_name(value, current="", previous="") -> str:
    mapping = {
        0: "无",
        1: "调高",
        2: "调低",
        3: "维持",
        4: "首次",
        "0": "无",
        "1": "调高",
        "2": "调低",
        "3": "维持",
        "4": "首次",
    }
    if value in mapping:
        return mapping[value]
    if current and not previous:
        return "首次"
    if current and previous and current == previous:
        return "维持"
    return "" if value is None else str(value)


def map_stock_item(item: dict, current_year=None) -> dict:
    info_code = item.get("infoCode", "")
    legacy_this_eps = item.get("predictThisYearEps", "") if current_year == 2024 else ""
    legacy_this_pe = item.get("predictThisYearPe", "") if current_year == 2024 else ""
    legacy_next_eps = item.get("predictNextYearEps", "") if current_year == 2024 else ""
    legacy_next_pe = item.get("predictNextYearPe", "") if current_year == 2024 else ""
    return {
        "股票代码": str(item.get("stockCode") or "").zfill(6),
        "股票简称": item.get("stockName", ""),
        "东财评级": item.get("emRatingName") or item.get("sRatingName") or "",
        "评级变动": rating_change_name(item.get("ratingChange"), item.get("emRatingName"), item.get("lastEmRatingName")),
        "机构": item.get("orgSName") or item.get("orgName") or "",
        "近一月个股研报数": item.get("count", ""),
        "2024盈利预测": legacy_this_eps,
        "2024市盈率预测": legacy_this_pe,
        "2025盈利预测": legacy_next_eps,
        "2025市盈率预测": legacy_next_pe,
        "预测当年": current_year or "",
        "预测年份口径": "api.currentYear" if current_year else "unverified",
        "当年盈利预测": item.get("predictThisYearEps", ""),
        "当年市盈率预测": item.get("predictThisYearPe", ""),
        "次年盈利预测": item.get("predictNextYearEps", ""),
        "次年市盈率预测": item.get("predictNextYearPe", ""),
        "后年盈利预测": item.get("predictNextTwoYearEps", ""),
        "后年市盈率预测": item.get("predictNextTwoYearPe", ""),
        "行业": item.get("indvInduName") or item.get("industryName") or "",
        "日期": clean_date(item.get("publishDate")),
        "报告名称": item.get("title", ""),
        "报告链接": report_link(KINDS["stock"], info_code),
        "原始文本": pd.NA,
    }


def map_industry_item(item: dict) -> dict:
    info_code = item.get("infoCode", "")
    return {
        "行业": item.get("industryName") or item.get("indvInduName") or "",
        "涨跌幅": "",
        "东财评级": item.get("emRatingName") or item.get("sRatingName") or "",
        "评级变动": rating_change_name(item.get("ratingChange"), item.get("emRatingName"), item.get("lastEmRatingName")),
        "机构": item.get("orgSName") or item.get("orgName") or "",
        "近一月行业研报数": item.get("count", ""),
        "日期": clean_date(item.get("publishDate")),
        "报告名称": item.get("title", ""),
        "报告链接": report_link(KINDS["industry"], info_code),
        "原始文本": pd.NA,
    }


def map_listing_item(kind: ReportKind, item: dict) -> dict:
    if kind.name == "stock":
        return map_stock_item(item)
    return map_industry_item(item)


def iter_date_ranges(start: date, end: date, chunk_days: int):
    cur = start
    step = max(1, chunk_days)
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=step - 1))
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def fetch_listing(
    kind: ReportKind,
    start: date,
    end: date,
    *,
    page_size: int,
    chunk_days: int,
    sleep: float,
    retries: int,
    limit_pages: int,
) -> pd.DataFrame:
    rows = []
    session = requests.Session()
    session.headers.update(HEADERS)
    for begin, finish in iter_date_ranges(start, end, chunk_days):
        page = 1
        while True:
            params = {
                "industryCode": "*",
                "pageSize": page_size,
                "industry": "*",
                "rating": "",
                "ratingChange": "",
                "beginTime": begin.isoformat(),
                "endTime": finish.isoformat(),
                "pageNo": page,
                "fields": "",
                "qType": kind.q_type,
                "orgCode": "",
                "code": "*",
                "rcode": "",
            }
            response = request_with_retry(session, REPORT_API, retries=retries, params=params)
            payload = response.json()
            data = payload.get("data") or []
            if not data:
                break
            rows.extend(map_stock_item(item, payload.get("currentYear")) if kind.name == "stock" else map_listing_item(kind, item) for item in data)
            print(f"[{kind.name}] {begin}..{finish} page {page}, rows {len(data)}, hits {payload.get('hits')}")
            if len(data) < page_size:
                break
            if limit_pages and page >= limit_pages:
                break
            page += 1
            time.sleep(sleep)
    if not rows:
        return pd.DataFrame(columns=kind.columns)
    return pd.DataFrame(rows, columns=kind.columns)


def has_text(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.strip().lower() != "nan"


def has_detail_status(row, *statuses: str) -> bool:
    value = row.get("详情页状态", "")
    return isinstance(value, str) and value in statuses


def set_detail_status(df: pd.DataFrame, idx, row: pd.Series, status: str):
    now = datetime.now().isoformat(timespec="seconds")
    for col in INTERNAL_COLUMNS:
        if col not in df.columns:
            df[col] = pd.NA
    df.loc[idx, "详情页状态"] = status
    df.loc[idx, "详情页状态时间"] = now
    row["详情页状态"] = status
    row["详情页状态时间"] = now


def dedupe_reports(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "报告链接" not in df.columns:
        return df
    work = df.copy()
    work["__has_text"] = work["原始文本"].apply(has_text) if "原始文本" in work.columns else False
    work["__has_detail_status"] = work.get("详情页状态", pd.Series(index=work.index, dtype=object)).apply(has_text)
    work["__date"] = pd.to_datetime(work.get("日期"), errors="coerce")
    work = work.sort_values(["报告链接", "__has_text", "__has_detail_status", "__date"], ascending=[True, False, False, False])
    work = work.drop_duplicates(subset=["报告链接"], keep="first")
    work = work.drop(columns=["__has_text", "__has_detail_status", "__date"], errors="ignore")
    work = work.sort_values("日期", ascending=False, na_position="last").reset_index(drop=True)
    return work


def infer_start_date(existing: pd.DataFrame, explicit: str | None, lookback_days: int) -> date:
    if explicit:
        return datetime.strptime(explicit, "%Y-%m-%d").date()
    if not existing.empty and "日期" in existing.columns:
        dates = pd.to_datetime(existing["日期"], errors="coerce")
        if dates.notna().any():
            return (dates.max().date() - timedelta(days=max(0, lookback_days)))
    return date.today() - timedelta(days=730)


def truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    encoded = bytearray()
    chars = []
    for char in text:
        chunk = char.encode("utf-8")
        if len(encoded) + len(chunk) > max_bytes:
            break
        encoded.extend(chunk)
        chars.append(char)
    return "".join(chars).rstrip()


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = str(name)
    for char in INVALID_FILENAME_CHARS:
        name = name.replace(char, "")
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    suffix = ".pdf" if name.lower().endswith(".pdf") else ""
    stem = name[: -len(suffix)] if suffix else name
    max_stem_bytes = max(1, max_len - len(suffix.encode("utf-8")))
    if len(name.encode("utf-8")) > max_len:
        stem = truncate_utf8_bytes(stem, max_stem_bytes)
        name = (stem + suffix).strip().strip(".")
    return name or "report"


def info_code_from_url(url: str) -> str:
    text = str(url)
    match = re.search(r"infocode=([^&]+)", text, flags=re.I)
    if match:
        return match.group(1)
    match = re.search(r"/(AP\d+)\.html", text, flags=re.I)
    if match:
        return match.group(1)
    match = re.search(r"(AP\d{12,})", text, flags=re.I)
    return match.group(1) if match else ""


def pdf_names(kind: ReportKind, row: pd.Series) -> list[str]:
    info_code = info_code_from_url(row.get("报告链接", ""))
    date_text = clean_date(row.get("日期"))
    org = row.get("机构", "")
    title = row.get("报告名称", "")
    if kind.name == "stock":
        code = str(row.get("股票代码", "")).zfill(6)
        stock = row.get("股票简称", "")
        legacy = sanitize_filename(f"zw_stock_{code}_{stock}_{org}_{date_text}.pdf")
        current = sanitize_filename(f"{info_code}_{code}_{stock}_{org}_{date_text}.pdf")
    else:
        industry = row.get("行业", "")
        legacy = sanitize_filename(f"{industry}_{date_text}_{org}_{title}.pdf")
        current = sanitize_filename(f"{info_code}_{industry}_{date_text}_{org}_{title}.pdf")
    return [name for name in [current, legacy] if name and name != ".pdf"]


def pdf_exists(kind: ReportKind, row: pd.Series) -> bool:
    folder = Path(kind.pdf_dir)
    return any((folder / name).exists() for name in pdf_names(kind, row))


def primary_pdf_path(kind: ReportKind, row: pd.Series) -> Path:
    return Path(kind.pdf_dir) / pdf_names(kind, row)[0]


def normalize_detail_text(element) -> str:
    paragraphs = []
    for node in element.xpath(".//p"):
        text = " ".join(node.text_content().split())
        if text:
            paragraphs.append(text)
    if paragraphs:
        return "\n".join(paragraphs)
    return "\n".join(line.strip() for line in element.text_content().splitlines() if line.strip())


def absolute_url(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    return url


def fetch_detail(session: requests.Session, kind: ReportKind, url: str, retries: int) -> tuple[str, str]:
    response = request_with_retry(session, url, retries=retries)
    html_text = response.text
    tree = lxml.html.fromstring(html_text)
    nodes = tree.xpath('//*[@id="ctx-content"] |//*[contains(concat(" ", normalize-space(@class), " "), " ctx-content ")]')
    text = normalize_detail_text(nodes[0]) if nodes else ""
    pdf_links = tree.xpath('//a[contains(concat(" ", normalize-space(@class), " "), " pdf-link ")]/@href')
    pdf_url = absolute_url(pdf_links[0]) if pdf_links else ""
    if not pdf_url:
        match = re.search(r'"attach_url"\s*:\s*"([^"]+)"', html_text)
        if match:
            pdf_url = absolute_url(match.group(1).replace("\\/", "/"))
    return text, pdf_url


def download_pdf(session: requests.Session, url: str, path: Path, retries: int) -> bool:
    if not url:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    response = request_with_retry(session, url, retries=retries, stream=True)
    content = response.content
    if len(content) < 1000:
        print(f"[warn] PDF too small: {url}")
        return False
    from report_attachments import validate_download
    try:
        attachment_format = validate_download(content)
    except Exception as exc:
        print(f"[warn] invalid report attachment: {type(exc).__name__}: {exc}")
        return False
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "wb") as file:
        file.write(content)
    os.replace(tmp_path, path)
    if attachment_format != "pdf":
        print(f"[attachment] native format={attachment_format}; extraction will bypass PDF OCR")
    return True


def write_excel_atomic(df: pd.DataFrame, path: str | Path):
    target = Path(path)
    tmp_path = target.with_name(f".{target.stem}.tmp{target.suffix}")
    df.to_excel(tmp_path, index=False)
    os.replace(tmp_path, target)


def save_outputs(kind: ReportKind, df: pd.DataFrame, checkpoint_index: int = 0, *, include_main: bool = True):
    output_df = df[[col for col in kind.columns if col in df.columns]]
    if include_main:
        write_excel_atomic(output_df, kind.list_file)
        write_excel_atomic(output_df, kind.detail_file)
    write_excel_atomic(output_df, f"{kind.checkpoint_prefix}_{checkpoint_index % 3}.xlsx")


def update_details(kind: ReportKind, df: pd.DataFrame, args) -> pd.DataFrame:
    session = requests.Session()
    session.headers.update(HEADERS)
    candidates = []
    for idx, row in df.iterrows():
        detail_not_found = has_detail_status(row, "not_found")
        detail_has_no_text = has_detail_status(row, "no_text")
        need_text = not detail_not_found and not detail_has_no_text and not has_text(row.get("原始文本"))
        need_pdf = not detail_not_found and not args.no_pdf and not pdf_exists(kind, row)
        if need_text or need_pdf:
            candidates.append((idx, need_text, need_pdf))
    if args.limit_details:
        candidates = candidates[: args.limit_details]
    print(f"[{kind.name}] detail/pdf pending: {len(candidates)}")
    checkpoint_index = 0
    progress = tqdm(candidates, desc=f"{kind.name} details", dynamic_ncols=True, mininterval=1)
    for done, (idx, need_text, need_pdf) in enumerate(progress, start=1):
        row = df.loc[idx]
        try:
            text, pdf_url = fetch_detail(session, kind, row["报告链接"], args.retries)
            changed = False
            if need_text and text:
                df.loc[idx, "原始文本"] = text
                changed = True
            elif need_text and not text:
                set_detail_status(df, idx, row, "no_text")
                changed = True
            if need_pdf and pdf_url:
                ok = download_pdf(session, pdf_url, primary_pdf_path(kind, row), args.retries)
                if not ok:
                    print(f"[warn] PDF download failed: {row['报告链接']}")
        except NotFoundError as exc:
            set_detail_status(df, idx, row, "not_found")
            print(f"[skip] detail not found: {row.get('报告链接')} -> {exc}")
        except Exception as exc:
            print(f"[warn] detail failed: {row.get('报告链接')} -> {type(exc).__name__}: {exc}")
        if args.checkpoint_every and done % args.checkpoint_every == 0:
            save_outputs(kind, df, checkpoint_index, include_main=False)
            tqdm.write(f"[{kind.name}] checkpoint saved: {kind.checkpoint_prefix}_{checkpoint_index % 3}.xlsx at {done}/{len(candidates)}")
            checkpoint_index += 1
        time.sleep(args.sleep)
    return df


def update_details_sqlite(kind: ReportKind, df: pd.DataFrame, args, conn: sqlite3.Connection) -> pd.DataFrame:
    session = requests.Session()
    session.headers.update(HEADERS)
    candidates = []
    for idx, row in df.iterrows():
        detail_not_found = has_detail_status(row, "not_found")
        detail_has_no_text = has_detail_status(row, "no_text")
        need_text = not detail_not_found and not detail_has_no_text and not has_text(row.get("原始文本"))
        need_pdf = not detail_not_found and not args.no_pdf and not pdf_exists(kind, row)
        if need_text or need_pdf:
            candidates.append((idx, need_text, need_pdf))
    if args.limit_details:
        candidates = candidates[: args.limit_details]
    print(f"[{kind.name}] detail/pdf pending: {len(candidates)}")
    progress = tqdm(candidates, desc=f"{kind.name} details", dynamic_ncols=True, mininterval=1)
    for done, (idx, need_text, need_pdf) in enumerate(progress, start=1):
        row = df.loc[idx].copy()
        try:
            text, pdf_url = fetch_detail(session, kind, row["报告链接"], args.retries)
            changed = False
            if need_text and text:
                df.loc[idx, "原始文本"] = text
                row["原始文本"] = text
                set_detail_status(df, idx, row, "ok")
                changed = True
            elif need_text and not text:
                set_detail_status(df, idx, row, "no_text")
                changed = True
            if need_pdf and pdf_url:
                ok = download_pdf(session, pdf_url, primary_pdf_path(kind, row), args.retries)
                if not ok:
                    print(f"[warn] PDF download failed: {row['报告链接']}")
                changed = changed or ok
            if changed or done % 100 == 0:
                update_report_sqlite(conn, kind, df.loc[idx])
        except NotFoundError as exc:
            set_detail_status(df, idx, row, "not_found")
            update_report_sqlite(conn, kind, df.loc[idx])
            print(f"[skip] detail not found: {row.get('报告链接')} -> {exc}")
        except Exception as exc:
            print(f"[warn] detail failed: {row.get('报告链接')} -> {type(exc).__name__}: {exc}")
        time.sleep(args.sleep)
    return df


def update_kind(kind: ReportKind, args):
    Path(kind.pdf_dir).mkdir(exist_ok=True)
    conn = connect_sqlite(args.db_file) if args.storage == "sqlite" else None
    existing = load_existing_sqlite(conn, kind, args.db_file) if conn else load_existing(kind)
    start = infer_start_date(existing, args.start_date, args.lookback_days)
    end = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    if start > end:
        start = end
    print(f"[{kind.name}] existing rows: {len(existing)}, listing range: {start}..{end}")
    listing = fetch_listing(
        kind,
        start,
        end,
        page_size=args.page_size,
        chunk_days=args.chunk_days,
        sleep=args.sleep,
        retries=args.retries,
        limit_pages=args.limit_list_pages,
    )
    combined = dedupe_reports(pd.concat([listing, existing], ignore_index=True))
    for col in kind.columns:
        if col not in combined.columns:
            combined[col] = pd.NA
    for col in INTERNAL_COLUMNS:
        if col not in combined.columns:
            combined[col] = pd.NA
    combined = combined[list(kind.columns) + INTERNAL_COLUMNS]
    print(f"[{kind.name}] fetched listing rows: {len(listing)}, merged rows: {len(combined)}")
    if args.dry_run:
        missing_text = int(combined["原始文本"].apply(lambda value: not has_text(value)).sum()) if "原始文本" in combined else 0
        print(f"[{kind.name}] dry-run: not writing files. missing text rows: {missing_text}")
        if conn:
            conn.close()
        return
    if conn:
        upsert_reports_sqlite(conn, kind, combined)
    if not args.list_only:
        if conn:
            combined = update_details_sqlite(kind, combined, args, conn)
        else:
            combined = update_details(kind, combined, args)
    if conn:
        if args.export_excel:
            latest = load_existing_sqlite(conn, kind, args.db_file)
            save_outputs(kind, latest, include_main=True)
            print(f"[{kind.name}] saved: {args.db_file}, {kind.list_file}, {kind.detail_file}")
        else:
            print(f"[{kind.name}] saved: {args.db_file}")
        conn.close()
    else:
        save_outputs(kind, combined)
        print(f"[{kind.name}] saved: {kind.list_file}, {kind.detail_file}")


def main(argv: list[str] | None = None, default_kinds: Iterable[str] = ("stock", "industry")):
    args = parse_args(argv, default_kinds=default_kinds)
    for kind_name in args.kinds:
        update_kind(KINDS[kind_name], args)


if __name__ == "__main__":
    main()
