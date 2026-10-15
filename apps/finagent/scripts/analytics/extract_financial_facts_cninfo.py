#!/usr/bin/env python3
"""Extract typed actual financial facts from official CNINFO report PDFs."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
PIPELINE_DIR = ROOT / "scripts/pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from common import connect  # noqa: E402
from v3_ops import record_quality, set_dataset_watermark, stage_run  # noqa: E402
from cninfo_statement_parser import parse_statements, parse_annual_prospectus  # noqa: E402
from cninfo_summary_parser import parse_summary, issuer_identity, VERSION as SUMMARY_VERSION  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")
NUMBER_RE = re.compile(r"(?<![\d.])(?:\([\d,]+(?:\.\d+)?\)|[-−]?\d[\d,]*(?:\.\d+)?)")
METRICS = (
    ("net_profit_parent", "归母净利润", ("归属于上市公司股东的净利润",)),
    ("revenue", "营业收入", ("营业收入",)),
    ("eps_basic", "基本每股收益", ("基本每股收益",)),
)


def decimal_value(raw: str) -> Decimal | None:
    value = raw.replace(",", "").replace("−", "-").strip()
    negative = value.startswith("(") and value.endswith(")")
    value = value.strip("()")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return -parsed if negative else parsed


def unit_factor(lines: list[str], index: int) -> tuple[Decimal | None, str | None]:
    context = "".join(lines[max(0, index - 35):index + 1])
    matches = list(re.finditer(r"单位[:：]?\s*(亿元|万元|千元|元)", context))
    if not matches:
        return None, None
    unit = matches[-1].group(1)
    return {
        "元": Decimal("1"),
        "千元": Decimal("1000"),
        "万元": Decimal("10000"),
        "亿元": Decimal("100000000"),
    }[unit], unit


def line_payload(lines: list[str], index: int) -> str:
    payload = lines[index].strip()
    if len(NUMBER_RE.findall(payload)) < 2:
        payload += " " + " ".join(line.strip() for line in lines[index + 1:index + 3])
    return payload


def parse_summary_facts(text: str) -> list[dict[str, object]]:
    return parse_summary(text)[0]


def parse_official_facts(text, report_end):
    summaries, warnings = parse_summary(text, report_end)
    try:
        statements, checks = parse_statements(text, report_end)
    except ValueError as exc:
        # An independently scoped summary is valid evidence even when another
        # statement cannot be parsed; never fall back to arbitrary PDF lines.
        statements, checks = [], []
        warnings.append({"reason": "statement_not_verified", "detail": str(exc)[:300]})
    # The old statement reader's optional summary branch does not preserve
    # missing column positions. Only the scoped summary reader supplies these.
    found = {(f['metric_code'], f.get('period_end', report_end)): f for f in statements if f.get('statement_section') != 'summary'}
    for fact in summaries:
        key = (fact['metric_code'], report_end)
        old = found.get(key)
        if old and old['value'] != fact['value']:
            warnings.append({"metric_code":fact['metric_code'],"reason":"statement_summary_conflict"})
            found.pop(key, None)
            continue
        found[key] = fact
        point = fact['period_type'] == 'point_in_time'
        previous_date = dt.date(report_end.year-1,12,31) if point else report_end.replace(year=report_end.year-1)
        previous_key = (fact['metric_code'],previous_date)
        if previous_key not in found:
            found[previous_key] = {**fact,'value':Decimal(fact['prior_value']),'yoy':None,
                'period_end':previous_date,'period_start':None if point else dt.date(previous_date.year,1,1),
                'comparative':True}
    return list(found.values()), checks, warnings


def pdf_text(path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
        check=True, capture_output=True, text=True, timeout=180,
    )
    return result.stdout


def store_pdf_text(conn, document_id, text: str) -> bool:
    """Retain extracted official text for search without replacing existing bodies."""
    if not text.strip():
        return False
    result = conn.execute(
        """
        UPDATE corpus.documents
        SET content_text=%s,
            metadata=coalesce(metadata,'{}'::jsonb)
              || '{"body_parser":"pdftotext:layout:utf8"}'::jsonb,
            updated_at=now()
        WHERE id=%s
          AND coalesce(nullif(btrim(content_text),''),
                       nullif(btrim(content_md),''),nullif(btrim(raw_text),''),'')=''
        """,
        (text, document_id),
    )
    return result.rowcount == 1


def period_type(filing_type: str, period_end: dt.date) -> str:
    if filing_type == "annual_report" or period_end.month == 12:
        return "annual"
    return "year_to_date"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--partition-key", default="default")
    parser.add_argument("--stock-code", help="Restrict an explicit company backfill; use a separate partition.")
    parser.add_argument("--filing-id-file",type=Path,help="JSON array of explicit filing UUIDs for a bounded repair retry.")
    parser.add_argument("--include-prospectus", action="store_true", help="Also parse three-year consolidated IPO statements.")
    parser.add_argument("--audit-dir", type=Path, help="Save exact pre-update facts and per-filing outcomes.")
    parser.add_argument("--repair-existing", action="store_true", help="Retire unverified legacy facts not recovered from a scoped table.")
    args = parser.parse_args()
    filing_ids=json.loads(args.filing_id_file.read_text()) if args.filing_id_file else None
    until = dt.date.fromisoformat(args.until) if args.until else dt.datetime.now(TZ).date()
    since = (
        dt.date.fromisoformat(args.since)
        if args.since else until - dt.timedelta(days=max(1, args.lookback_days) - 1)
    )
    if since > until:
        parser.error("--since cannot be after --until")
    if args.repair_existing and not args.audit_dir:
        parser.error("--repair-existing requires --audit-dir")
    if args.audit_dir:
        args.audit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    with stage_run(
        "finance_v3_morning", "financial_facts_cninfo", expected_date=until,
        metadata={
            "since": since.isoformat(), "until": until.isoformat(),
            "partition_key": args.partition_key,
        },
    ) as run:
        with connect(admin=False) as conn:
            filings = conn.execute(
                """
                SELECT f.filing_id,f.asset_id,f.stock_code,f.filing_type,f.title,
                       f.period_end,f.announced_at,f.available_at,f.revision_no,
                       f.document_id,f.source_external_id,df.file_path
                FROM company.filings f
                JOIN corpus.document_files df
                  ON df.document_id=f.document_id AND df.file_role='pdf'
                WHERE f.source_name='cninfo'
                  AND ((f.filing_type IN ('annual_report','semiannual_report','quarterly_report')
                        AND f.period_end IS NOT NULL)
                       OR (%s AND f.title LIKE '%%招股说明书'))
                  AND f.announced_at::date BETWEEN %s AND %s
                  AND (%s::text IS NULL OR f.stock_code=%s)
                  AND (%s::uuid[] IS NULL OR f.filing_id=ANY(%s::uuid[]))
                ORDER BY f.announced_at,f.source_external_id
                LIMIT %s
                """,
                (args.include_prospectus, since, until, args.stock_code, args.stock_code, filing_ids,filing_ids,max(1, args.limit)),
            ).fetchall()
            parsed_documents = 0
            inserted_facts = 0
            errors: list[dict[str, str]] = []
            for filing in filings:
                outcome = {"filing_id":str(filing['filing_id']),"stock_code":filing['stock_code'],"document_id":str(filing['document_id'])}
                try:
                    path = Path(filing["file_path"])
                    if not path.exists() or not filing["asset_id"]:
                        raise ValueError("official report PDF or asset mapping is unavailable")
                    text = pdf_text(path)
                    store_pdf_text(conn, filing["document_id"], text)
                    issuer_check = issuer_identity(text, filing['stock_code'])
                    if issuer_check['conflict']:
                        raise ValueError('financial report issuer differs from filing owner: ' + str(issuer_check))
                    is_prospectus = filing['title'].endswith('招股说明书')
                    if is_prospectus:
                        facts, statement_checks = parse_annual_prospectus(text)
                        parse_warnings = []
                    else:
                        facts, statement_checks, parse_warnings = parse_official_facts(text, filing["period_end"])
                    if args.audit_dir:
                        previous = conn.execute("SELECT * FROM company.financial_facts WHERE filing_id=%s AND source_name='cninfo_official'", (filing['filing_id'],)).fetchall()
                        backup = args.audit_dir / (str(filing['filing_id']) + '.before.json')
                        if not backup.exists():
                            backup.write_text(json.dumps(previous,ensure_ascii=False,default=str))
                    if args.repair_existing:
                        conn.execute("""UPDATE company.financial_facts SET quality_status='unverified',
                            confidence=least(confidence,0.5),metadata=metadata || '{"revalidation_required":true,"revalidation_reason":"legacy_unscoped_parser"}'::jsonb
                            WHERE filing_id=%s AND source_name='cninfo_official'
                              AND (metadata->>'parser'='cninfo_financial_summary:v1'
                                OR (metadata->>'parser' LIKE 'cninfo_scoped_summary:%%' AND metadata->>'parser'<>%s))""", (filing['filing_id'],SUMMARY_VERSION))
                    if not facts:
                        conn.commit()
                        raise ValueError("supported summary financial facts were not parsed")
                    report_end = filing['period_end'] or max(fact['period_end'] for fact in facts)
                    for fact in facts:
                        conn.execute(
                            """
                            INSERT INTO company.financial_facts (
                              filing_id,asset_id,stock_code,metric_code,metric_name,fact_kind,
                              statement_scope,period_type,period_start,period_end,value,yoy,unit,
                              currency,announced_at,available_at,revision_no,source_name,
                              source_document_id,source_text,confidence,quality_status,metadata
                            ) VALUES (
                              %s,%s,%s,%s,%s,'actual','consolidated',%s,%s,%s,%s,%s,%s,'CNY',
                              %s,%s,%s,'cninfo_official',%s,%s,1.0,'verified',%s
                            )
                            ON CONFLICT (
                              filing_id,asset_id,metric_code,fact_kind,statement_scope,
                              period_end,revision_no,source_name
                            ) DO UPDATE SET
                              value=EXCLUDED.value,yoy=EXCLUDED.yoy,unit=EXCLUDED.unit,
                              period_type=EXCLUDED.period_type,period_start=EXCLUDED.period_start,
                              announced_at=EXCLUDED.announced_at,available_at=EXCLUDED.available_at,
                              source_document_id=EXCLUDED.source_document_id,
                              source_text=EXCLUDED.source_text,confidence=EXCLUDED.confidence,
                              quality_status=EXCLUDED.quality_status,metadata=EXCLUDED.metadata
                            """,
                            (
                                filing["filing_id"], filing["asset_id"], filing["stock_code"],
                                fact["metric_code"], fact["metric_name"],
                                fact.get("period_type", period_type(filing["filing_type"], report_end)),
                                fact.get("period_start", dt.date(report_end.year, 1, 1)),
                                fact.get("period_end", report_end),
                                fact["value"], fact["yoy"], fact["unit"],
                                filing["announced_at"], filing["available_at"],
                                filing["revision_no"], filing["document_id"], fact["source_text"],
                                Jsonb({
                                    "parser": fact.get("parser", "cninfo_consolidated_statements:v3"),
                                    "official_source": True,
                                    "issuer_identity": issuer_check,
                                    "source_unit": fact["source_unit"],
                                    "pdf_path": str(path),
                                    "statement_section": fact.get("statement_section", "legacy_summary"),
                                    "comparative": fact.get("comparative", False),
                                    "comparative_basis":fact.get('comparative_basis','reported'),
                                    "identity_checks": statement_checks,
                                    "parse_warnings": parse_warnings,
                                    "field_checks": fact.get("field_checks", {"scope":"consolidated_statement","explicit_unit":True,"period_columns":True}),
                                    "source_page": fact.get("source_page"),
                                    "source_line": fact.get("source_line"),
                                    "audit_status": "audited" if is_prospectus and "经审计财务报表" in text
                                      else "unaudited" if "未经审计" in text else "unknown",
                                }),
                            ),
                        )
                        inserted_facts += 1
                    parsed_documents += 1
                    conn.commit()
                    outcome.update(status="parsed",facts=len(facts),warnings=parse_warnings)
                except Exception as exc:  # noqa: BLE001
                    conn.rollback()
                    errors.append({
                        "external_id": str(filing["source_external_id"]),
                        "error": f"{type(exc).__name__}: {exc}"[:400],
                    })
                    outcome.update(status="failed",error=f"{type(exc).__name__}: {exc}"[:400])
                if args.audit_dir:
                    with (args.audit_dir / 'outcomes.jsonl').open('a') as stream:
                        stream.write(json.dumps(outcome,ensure_ascii=False,default=str)+'\n')
                if (parsed_documents + len(errors)) % 100 == 0:
                    print({"processed":parsed_documents+len(errors),"parsed":parsed_documents,"facts":inserted_facts,"errors":len(errors)},flush=True)

            summary = conn.execute(
                """
                SELECT count(*) AS rows,count(DISTINCT asset_id) AS assets,
                       max(available_at) AS data_through,
                       count(*) FILTER (
                         WHERE fact_kind='actual' AND quality_status='verified'
                           AND source_name='cninfo_official'
                       ) AS official_verified
                FROM company.financial_facts
                """
            ).fetchone()

        eligible = len(filings)
        coverage = 100.0 * parsed_documents / eligible if eligible else 100.0
        quality = "pass" if eligible and coverage >= 99 else "warn"
        record_quality(
            run["run_id"], "company.financial_facts", "official_pdf_parse_coverage",
            status="pass" if quality == "pass" else "warn", severity="warning",
            partition_key=args.partition_key, observed_value=coverage,
            threshold_value=99, affected_rows=eligible - parsed_documents,
            sample=errors[:20], details={
                "eligible_pdfs": eligible, "parsed_documents": parsed_documents,
                "inserted_facts": inserted_facts,
                "official_verified_total": summary["official_verified"],
            },
        )
        set_dataset_watermark(
            "company.financial_facts", schema_version="v3", run_id=run["run_id"],
            partition_key=args.partition_key, expected_date=until,
            data_through=summary["data_through"], row_count=summary["rows"],
            eligible_count=summary["assets"], coverage_pct=coverage,
            quality_status=quality, source_version=SUMMARY_VERSION,
            details={
                "since": since.isoformat(), "until": until.isoformat(),
                "official_verified": summary["official_verified"],
                "errors": errors[:20],
            },
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {
            f"company.financial_facts:{args.partition_key}": until.isoformat()
        }
    print({
        "eligible_pdfs": eligible, "parsed_documents": parsed_documents,
        "inserted_facts": inserted_facts, "errors": len(errors),
        "coverage_pct": round(coverage, 3), "quality_status": quality,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
