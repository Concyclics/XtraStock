#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


INVALID_FILENAME_CHARS = r'\/:*?"<>|'
INFO_CODE_RE = re.compile(r"infocode=([^&]+)", re.I)
AP_CODE_RE = re.compile(r"(AP\d{12,})", re.I)


@dataclass(frozen=True)
class PreparedReport:
    source_type: str
    source_name: str
    external_id: str
    report_type: str
    title: str
    publisher: str
    published_at: str
    url: str
    language: str
    content_text: str
    content_md: str
    raw_text: str
    content_hash: str
    report_date: str
    institution: str
    rating: str
    rating_change: str
    industry_name: str
    stock_code: str
    stock_name: str
    source_url: str
    web_text_status: str
    pdf_status: str
    ocr_status: str
    pdf_path: str
    pdf_size_bytes: str
    ocr_md_path: str
    ocr_md_size_bytes: str
    ocr_quality_json_path: str
    ocr_quality_md_path: str
    ocr_quality: str
    forecast_current_year: str
    forecast_current_eps: str
    forecast_current_pe: str
    forecast_next_year: str
    forecast_next_eps: str
    forecast_next_pe: str
    forecast_later_year: str
    forecast_later_eps: str
    forecast_later_pe: str
    metadata: str


def truncate_utf8_bytes(text: str, max_bytes: int) -> str:
    encoded = bytearray()
    chars: list[str] = []
    for char in text:
        chunk = char.encode("utf-8")
        if len(encoded) + len(chunk) > max_bytes:
            break
        encoded.extend(chunk)
        chars.append(char)
    return "".join(chars).rstrip()


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = str(name or "")
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


def safe_ocr_stem(stem: str, max_bytes: int = 150) -> str:
    if len(stem.encode("utf-8")) <= max_bytes:
        return stem
    digest = hashlib.sha1(stem.encode("utf-8", errors="replace")).hexdigest()[:12]
    suffix = "_" + digest
    budget = max_bytes - len(suffix.encode("utf-8"))
    encoded = stem.encode("utf-8", errors="replace")[:budget]
    while encoded:
        try:
            return encoded.decode("utf-8").rstrip("._- ") + suffix
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return digest


def info_code_from_url(url: str) -> str:
    text = str(url or "")
    match = INFO_CODE_RE.search(text)
    if match:
        return match.group(1)
    match = AP_CODE_RE.search(text)
    return match.group(1) if match else ""


def normalize_date(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(value[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return value[:10]


def normalize_stock_code(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text.zfill(6)
    return text


def pdf_names(report_type: str, row: sqlite3.Row) -> list[str]:
    info_code = info_code_from_url(row["报告链接"])
    date_text = normalize_date(row["日期"])
    org = row["机构"] or ""
    title = row["报告名称"] or ""
    if report_type == "stock":
        code = normalize_stock_code(row["股票代码"])
        stock = row["股票简称"] or ""
        legacy = sanitize_filename(f"zw_stock_{code}_{stock}_{org}_{date_text}.pdf")
        current = sanitize_filename(f"{info_code}_{code}_{stock}_{org}_{date_text}.pdf")
    else:
        industry = row["行业"] or ""
        legacy = sanitize_filename(f"{industry}_{date_text}_{org}_{title}.pdf")
        current = sanitize_filename(f"{info_code}_{industry}_{date_text}_{org}_{title}.pdf")
    return [name for name in [current, legacy] if name and name != ".pdf"]


def first_existing(paths: list[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def resolve_paths(report_type: str, row: sqlite3.Row) -> dict[str, Path | None]:
    pdf_dir = Path("PDF") if report_type == "stock" else Path("PDF_ind")
    md_dir = Path("PDF_md") if report_type == "stock" else Path("PDF_ind_md")
    pdf_candidates = [pdf_dir / name for name in pdf_names(report_type, row)]
    pdf_path = first_existing(pdf_candidates)

    md_candidates: list[Path] = []
    quality_json_candidates: list[Path] = []
    quality_md_candidates: list[Path] = []
    for pdf_candidate in pdf_candidates:
        stem = pdf_candidate.stem
        for ocr_stem in dict.fromkeys([stem, safe_ocr_stem(stem)]):
            md_candidates.append(md_dir / f"{ocr_stem}.md")
            quality_json_candidates.append(md_dir / f"{ocr_stem}_quality.json")
            quality_md_candidates.append(md_dir / f"{ocr_stem}_quality.md")

    return {
        "pdf": pdf_path,
        "ocr_md": first_existing(md_candidates),
        "ocr_quality_json": first_existing(quality_json_candidates),
        "ocr_quality_md": first_existing(quality_md_candidates),
    }


def read_text(path: Path | None) -> str:
    if not path:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def read_quality(path: Path | None) -> tuple[str, str]:
    if not path:
        return "{}", "none"
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return json.dumps({"error": f"quality_parse_failed: {exc}"}, ensure_ascii=False), "partial"
    failed_pages = data.get("failed_pages") or []
    error = data.get("error")
    status = "partial" if failed_pages or error else "ok"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")), status


def file_size(path: Path | None) -> str:
    if not path:
        return ""
    try:
        return str(path.stat().st_size)
    except OSError:
        return ""


def sql_numeric(value: str) -> str:
    text = str(value or "").strip()
    if not text or text in {"-", "--", "—", "不适用"}:
        return ""
    text = text.replace(",", "").replace("倍", "").replace("%", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""


def content_hash(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update((part or "").encode("utf-8", errors="replace"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def prepare_stock(row: sqlite3.Row) -> PreparedReport:
    return prepare_report("stock", row)


def prepare_industry(row: sqlite3.Row) -> PreparedReport:
    return prepare_report("industry", row)


def prepare_report(report_type: str, row: sqlite3.Row) -> PreparedReport:
    info_code = info_code_from_url(row["报告链接"])
    report_date = normalize_date(row["日期"])
    paths = resolve_paths(report_type, row)
    md_text = read_text(paths["ocr_md"])
    quality_json, quality_status = read_quality(paths["ocr_quality_json"])
    raw_text = row["原始文本"] or ""
    web_text_status = row["详情页状态"] if "详情页状态" in row.keys() and row["详情页状态"] else ("ok" if raw_text else "")
    pdf_status = "downloaded" if paths["pdf"] else "missing"
    # A recorded conversion failure remains visible even when it produced no
    # Markdown. Previously these failures were relabelled as pending forever.
    ocr_status = quality_status if paths["ocr_md"] or quality_status == "partial" else "pending"
    if not paths["ocr_md"] and not paths["pdf"]:
        ocr_status = "none"

    if report_type == "stock":
        stock_code = normalize_stock_code(row["股票代码"])
        stock_name = row["股票简称"] or ""
        industry_name = row["行业"] or ""
        from analytics.forecast_semantics import api_years, extract_dated_forecasts, evidence_metadata
        forecast_current_year, forecast_next_year, forecast_later_year = api_years(dict(row))
        metadata = {
            "source_table": "stock_reports",
            "semantic_contract": "earnings-forecast-v2",
            "year_verified": bool(forecast_current_year),
            "year_basis": "api.currentYear" if forecast_current_year else "legacy_unverified",
            "eps_unit": "CNY/share",
            "pe_unit": "times",
            "monthly_report_count": row["近一月个股研报数"],
            "legacy_2024_profit_forecast": row["2024盈利预测"],
            "legacy_2024_pe_forecast": row["2024市盈率预测"],
            "legacy_2025_profit_forecast": row["2025盈利预测"],
            "legacy_2025_pe_forecast": row["2025市盈率预测"],
            "detail_status_time": row["详情页状态时间"] if "详情页状态时间" in row.keys() else None,
        }
        items, rejected = extract_dated_forecasts(raw_text, int(report_date[:4])) if report_date else ([], [])
        by_year = {}
        for item in items:
            by_year.setdefault(item['forecast_year'], []).append(item)
        metadata['dated_forecasts'] = [dict(forecast_year=year,
            **{item['field']: item['value'] for item in group},
            metadata=evidence_metadata(group, raw_text)) for year, group in by_year.items()]
        metadata['forecast_parse_rejections'] = rejected
        forecast_current_eps = sql_numeric(row["当年盈利预测"])
        forecast_current_pe = sql_numeric(row["当年市盈率预测"])
        forecast_next_eps = sql_numeric(row["次年盈利预测"])
        forecast_next_pe = sql_numeric(row["次年市盈率预测"])
        forecast_later_eps = sql_numeric(row["后年盈利预测"])
        forecast_later_pe = sql_numeric(row["后年市盈率预测"])
    else:
        stock_code = ""
        stock_name = ""
        industry_name = row["行业"] or ""
        metadata = {
            "source_table": "industry_reports",
            "change_pct": row["涨跌幅"],
            "monthly_report_count": row["近一月行业研报数"],
            "detail_status_time": row["详情页状态时间"] if "详情页状态时间" in row.keys() else None,
        }
        forecast_current_year = forecast_current_eps = forecast_current_pe = ""
        forecast_next_year = forecast_next_eps = forecast_next_pe = ""
        forecast_later_year = forecast_later_eps = forecast_later_pe = ""

    title = row["报告名称"] or info_code
    institution = row["机构"] or ""
    quality_data = json.loads(quality_json)
    if quality_status == "ok":
        if quality_data.get("source_sha256") and paths["pdf"]:
            actual = hashlib.sha256(paths["pdf"].read_bytes()).hexdigest()
            if actual != quality_data["source_sha256"]:
                raise ValueError(f"attachment changed after conversion: {paths['pdf']}")
        if quality_data.get("content_sha256") and paths["ocr_md"]:
            actual = hashlib.sha256(paths["ocr_md"].read_bytes()).hexdigest()
            if actual != quality_data["content_sha256"]:
                raise ValueError(f"attachment text checksum mismatch: {paths['ocr_md']}")
    # Preserve the web summary separately; retrieval must see the parsed
    # attachment body rather than only the earlier summary.
    body = md_text if md_text and quality_status == "ok" else (raw_text or md_text)
    if quality_data.get("extraction_method"):
        metadata["attachment_extraction"] = {key: quality_data[key] for key in
            ("attachment_format", "extraction_method", "parser_version", "source_sha256",
             "content_sha256", "sheet_count", "cell_count", "source_error_cells", "warnings",
             "omitted_objects", "search_full_body") if key in quality_data}
    if quality_data.get("error") or quality_data.get("failed_pages"):
        metadata["attachment_failure"] = {key: quality_data[key] for key in
            ("error", "failed_pages", "source_sha256", "repair_audit") if key in quality_data}
    digest = content_hash(info_code, title, raw_text, md_text, body)

    return PreparedReport(
        source_type="research_report",
        source_name="eastmoney",
        external_id=info_code,
        report_type=report_type,
        title=title,
        publisher=institution,
        published_at=report_date,
        url=row["报告链接"] or "",
        language="zh",
        content_text=body,
        content_md=md_text,
        raw_text=raw_text,
        content_hash=digest,
        report_date=report_date,
        institution=institution,
        rating=row["东财评级"] or "",
        rating_change=row["评级变动"] or "",
        industry_name=industry_name,
        stock_code=stock_code,
        stock_name=stock_name,
        source_url=row["报告链接"] or "",
        web_text_status=web_text_status,
        pdf_status=pdf_status,
        ocr_status=ocr_status,
        pdf_path=str(paths["pdf"] or ""),
        pdf_size_bytes=file_size(paths["pdf"]),
        ocr_md_path=str(paths["ocr_md"] or ""),
        ocr_md_size_bytes=file_size(paths["ocr_md"]),
        ocr_quality_json_path=str(paths["ocr_quality_json"] or ""),
        ocr_quality_md_path=str(paths["ocr_quality_md"] or ""),
        ocr_quality=quality_json,
        forecast_current_year=forecast_current_year,
        forecast_current_eps=forecast_current_eps,
        forecast_current_pe=forecast_current_pe,
        forecast_next_year=forecast_next_year,
        forecast_next_eps=forecast_next_eps,
        forecast_next_pe=forecast_next_pe,
        forecast_later_year=forecast_later_year,
        forecast_later_eps=forecast_later_eps,
        forecast_later_pe=forecast_later_pe,
        metadata=json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
    )


def rows(conn: sqlite3.Connection, table: str):
    conn.row_factory = sqlite3.Row
    query = f"SELECT * FROM {table} WHERE 报告链接 IS NOT NULL AND 报告链接 != '' ORDER BY 日期, 报告链接"
    yield from conn.execute(query)


def open_writer(output_path: Path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(PreparedReport.__dataclass_fields__.keys())
    handle = output_path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    return handle, writer


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Eastmoney research reports TSV for PostgreSQL COPY.")
    parser.add_argument("--sqlite-db", default="eastmoney_reports.sqlite3")
    parser.add_argument("--output", default="build/eastmoney_reports_import/eastmoney_reports.tsv")
    parser.add_argument("--kind", choices=["all", "stock", "industry"], default="all")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    output_path = Path(args.output).resolve()
    conn = sqlite3.connect(args.sqlite_db)
    conn.row_factory = sqlite3.Row
    handle, writer = open_writer(output_path)

    prepared = 0
    skipped: list[str] = []
    counts = {"stock": 0, "industry": 0}
    try:
        jobs = []
        if args.kind in {"all", "stock"}:
            jobs.append(("stock", "stock_reports", prepare_stock))
        if args.kind in {"all", "industry"}:
            jobs.append(("industry", "industry_reports", prepare_industry))
        for kind, table, prepare in jobs:
            for row in rows(conn, table):
                if args.limit and prepared >= args.limit:
                    break
                try:
                    writer.writerow(prepare(row).__dict__)
                    prepared += 1
                    counts[kind] += 1
                except Exception as exc:
                    skipped.append(f"{kind}\t{row['报告链接']}\t{exc}")
                if prepared and prepared % 10000 == 0:
                    print(f"processed={prepared} stock={counts['stock']} industry={counts['industry']} skipped={len(skipped)}")
            if args.limit and prepared >= args.limit:
                break
    finally:
        handle.close()
        conn.close()

    report_path = output_path.with_suffix(".report.json")
    report_path.write_text(
        json.dumps(
            {
                "sqlite_db": args.sqlite_db,
                "output": str(output_path),
                "kind": args.kind,
                "prepared_rows": prepared,
                "counts": counts,
                "skipped_rows": len(skipped),
                "skipped_samples": skipped[:50],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"prepared_rows={prepared} stock={counts['stock']} industry={counts['industry']} skipped_rows={len(skipped)} output={output_path}")
    print(f"report={report_path}")


if __name__ == "__main__":
    main()
