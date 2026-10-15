#!/usr/bin/env python3
"""Extract official equity-distribution facts from downloaded CNINFO PDFs."""
from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from common import connect
from corporate_action_versions import save_distribution

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from v3_ops import record_quality, set_dataset_watermark, stage_run  # noqa: E402


TZ = ZoneInfo("Asia/Shanghai")
IMPLEMENTATION_TITLE_RE = re.compile(r"(?:权益分派|利润分配|分红派息).{0,30}实施(?:的)?公告")
NUMBER = r"([0-9]+(?:\.[0-9]+)?)"
VALUE = r"[0-9]+(?:\.[0-9]+)?"
DATE_VALUE = r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?"


def normalize_text(value: str) -> str:
    return re.sub(r"[\s\u00a0\u3000]+", "", value).translate(str.maketrans({'–':'-','‒':'-','−':'-'}))


def parsed_date(parts: tuple[str, str, str]) -> dt.date | None:
    try:
        return dt.date(*(int(part) for part in parts))
    except ValueError:
        return None


def labeled_date(text: str, labels: tuple[str, ...]) -> dt.date | None:
    label_expr = "|".join(re.escape(label) for label in labels)
    # Require an explicit separator so a compacted table header does not bind
    # every label to the first date in the following row.
    match = re.search(rf"(?:{label_expr})(?:为\s*[:：]?|[:：])\s*{DATE_VALUE}", text)
    if not match:
        return None
    return parsed_date((match.group(1), match.group(2), match.group(3)))


def labeled_month_day(
    text: str, labels: tuple[str, ...], default_year: int | None
) -> dt.date | None:
    if not default_year:
        return None
    label_expr = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_expr})(?:为\s*[:：]?|[:：])\s*(\d{{1,2}})月(\d{{1,2}})日",
        text,
    )
    if not match:
        return None
    return parsed_date((str(default_year), match.group(1), match.group(2)))


def narrative_pay_date(text: str) -> dt.date | None:
    match = re.search(rf"现金红利(?:将于|于){DATE_VALUE}(?:通过|发放|划入)", text)
    if not match:
        return None
    return parsed_date((match.group(1), match.group(2), match.group(3)))


def first_ratio(text: str, patterns: tuple[str, ...]) -> float:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1)) / 10.0
    return 0.0


def first_per_share(text: str, patterns: tuple[str, ...]) -> float:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    return 0.0


def effective_share_ratio(text: str, declared: float) -> float | None:
    """Read a formula's completed ratio, never its numerator or a cash formula.

    CNINFO formulas contain comma-separated share counts (119,729,530),
    intermediate results and approximate results. The old unbounded wildcard
    read 119 from that numerator, or crossed into the next cash formula.
    A differential distribution cannot create more shares per entitled share
    than the declared combined distribution.
    """
    matches = list(re.finditer(
        r"(?=(?P<virtual>虚拟分派(?:的)?)?流通股份变动比例(?:为|[=:：＝])([^。；;]*))", text
    ))
    # A generic '股份变动比例为0.4' can describe the nominal distribution.
    # Prefer the explicitly diluted calculation when both are present.
    for match in sorted(matches, key=lambda m: not bool(m.group('virtual'))):
        clause = re.split(r"现金红利|根据|综上|因此|除权|三、",
                          match.group(2), maxsplit=1)[0]
        # Prefix '=' also handles a direct statement, e.g. 比例为0.3989.
        candidates = re.findall(
            rf"[=＝≈]{NUMBER}(?![\d.,，×*÷/＋+\-＝=≈])", "=" + clause
        )
        if candidates:
            value = float(candidates[-1])
            if 0 <= value <= declared + 1e-8:
                return value
    for m in re.finditer(r'除(?:权除)?息参考价(?:格)?[=＝]([^。；;]*)',text):
        values=re.findall(r'[/／÷][（(]1\+'+NUMBER+r'[）)]',m.group(1))
        valid=[float(v) for v in values if 0<=float(v)<=declared+1e-8]
        if valid:return valid[-1]
    return None


def effective_cash_ratio(text: str, declared: float) -> float | None:
    """Bound the cash formula to its clause and declared per-share amount."""
    cap=declared*1.1+1e-8
    # The reference price states the total-share diluted ratio, whereas an
    # earlier paragraph may state the slightly higher entitled-share payout.
    for m in re.finditer(r"除(?:权除)?息(?:参考)?价(?:格)?(?:[（(]元/股[）)])?(?:为)?[=:：＝]([^。；;]*)",text):
        values=re.findall(rf"收盘(?:价|价格)[-－﹣]{NUMBER}(?:元)?(?=[/／）)；;，,]|$)",m.group(1))
        valid=[float(v) for v in values if 0<=float(v)<=cap]
        if valid:return valid[-1]
    direct=re.search(rf"即每股现金红利{NUMBER}元",text)
    if direct and 0<=float(direct.group(1))<=cap:return float(direct.group(1))
    label=r"(?:即每股现金红利|虚拟分派(?:的)?(?:每股)?现金红利|按总股本折算(?:的)?每股现金红利|每股现金红利|每股现金分红(?:金额|比例)?)"
    for m in re.finditer(r"(?="+label+r"(?:[（(]含税[）)])?(?:为|[=＝:：])([^。；;]*))",text):
        clause=re.split(r"流通股份变动比例|每股送红股|每股转增|按(?:公司)?总股本.{0,8}每10股|按A股.{0,60}每10股|综上|因此|三、",m.group(1),maxsplit=1)[0]
        # Numeric cash-total / total-share operands are dimensionally one
        # yuan/share. Some original PDFs misprint a per-ten number as per-share;
        # keep the arithmetic, rather than copying that inconsistent label.
        operands=re.findall(r"(?<![\d.,/])([0-9][0-9,]*(?:\.[0-9]+)?)元[÷/／]([0-9][0-9,]*(?:\.[0-9]+)?)(?:股|(?=[=＝≈]))",clause)
        derived=None
        for numerator,denominator in operands:
            amount=float(numerator.replace(',',''));shares=float(denominator.replace(',',''))
            if amount>=10000 and shares>=100000:
                value=amount/shares
                if 0<=value<=max(20,declared*10):derived=value;break
        values=re.findall(rf"(?:[=＝≈]|即){NUMBER}(?:元|(?![\d.,，×*÷/＋+\-＝=≈]))",'='+clause)
        # Some issuers specify the diluted dividend solely as the subtraction
        # in the ex-dividend reference-price formula.
        if not values:
            values=re.findall(rf"收盘(?:价|价格)[-－﹣]{NUMBER}(?:元)?(?=[/／）)；;，,]|$)",clause)
        valid=[float(v) for v in values if 0<=float(v)<=cap]
        if derived is not None:
            consistent=[float(v) for v in values if abs(float(v)-derived)<=1e-6]
            return consistent[-1] if consistent else derived
        if valid:return valid[-1]
    m=re.search(rf"每股现金红利应以{NUMBER}元",text)
    if m and 0<=float(m.group(1))<=cap:return float(m.group(1))
    # Explicit ex-dividend reference subtraction is not a bond conversion or
    # repurchase-price formula. Keep it within its own sentence.
    for m in re.finditer(r"除(?:权除)?息(?:参考)?价(?:格)?[=＝]([^。；;]{0,220})",text):
        values=re.findall(rf"收盘(?:价|价格)[-－﹣]{NUMBER}元",m.group(1))
        valid=[float(v) for v in values if 0<=float(v)<=declared*1.5+1e-8]
        if valid:return valid[-1]
    return None


def declared_cash_amount(text: str) -> float:
    """Choose the first declared distribution, before later tax examples.

    Pattern-priority matching previously preferred a later QFII after-tax
    amount over an earlier gross '每10股派16元人民币现金'.
    """
    patterns=(
        (rf"每10股(?:派发|派送|派息|派)(?:人民币)?(?:现金)?(?:红利|股利)?(?:人民币)?{NUMBER}元",10.),
        (rf"每10股现金(?:分红|红利|股利){NUMBER}元",10.),
        (rf"每10股送(?:红股|股)?{VALUE}股[,，]?(?:同时)?派{NUMBER}元",10.),
        (rf"每股(?:派发)?现金(?:红利|股利)(?:人民币)?{NUMBER}元",1.),
        (rf"每股(?:A股)?(?:派发|派送|派)?(?:的)?现金(?:红利|股利|分红(?:金额)?)(?:为)?(?:[（(]含税[）)])?(?:人民币)?{NUMBER}(?:元|(?=[,，；;。]))",1.),
    )
    found=[]
    for pattern,unit in patterns:
        for m in re.finditer(pattern,text):
            found.append((m.start(),float(m.group(1))/unit))
    if found:return min(found)[1]
    # A layout-fragmented number can follow the entire sentence. Require a
    # fully numeric share basis and total cash in the same current-plan clause.
    amounts=re.search(r'(?:总股本|股份总数)([0-9,]+)股为基数[^。]{0,160}共计派发现金红利(?:人民币)?([0-9,]+(?:\.[0-9]+)?)元',text)
    if amounts:
        shares=float(amounts.group(1).replace(',',''));total=float(amounts.group(2).replace(',',''))
        if shares>100000 and 0<=total/shares<=20:return total/shares
    return 0.


def table_dates(text: str) -> tuple[dt.date | None, dt.date | None, dt.date | None]:
    # Keep ASCII spaces between adjacent table cells. Removing them turns
    # ``2021/5/6 2021/5/7`` into one ambiguous numeric token.
    lines = [re.sub(r"[\u00a0\u3000]+", " ", line) for line in text.splitlines()]
    for index, line in enumerate(lines):
        header = normalize_text("".join(lines[max(0, index - 2):index + 3]))
        if "股权登记" not in normalize_text(line) or "除权" not in header:
            continue
        # -layout retains horizontal coordinates. A shared A/B-share ex-date
        # can occupy a separate physical line. Reading the first date row by
        # ordinal position mistakenly took the new-share listing date instead.
        record_col=line.find('股权登记日');ex_col=line.find('除权')
        if record_col>=0 and ex_col-record_col>=8:
            for offset,candidate in enumerate(lines[index+1:index+7],start=index+1):
                tokens=list(re.finditer(DATE_VALUE,candidate))
                if len(tokens)<3:continue
                record_token=min(tokens,key=lambda m:abs(m.start()-record_col))
                if abs(record_token.start()-record_col)>5:continue
                combined=list(tokens)
                for extra_line in lines[offset+1:offset+3]:
                    if extra_line.lstrip().startswith(('B股','Ｂ股','B 股','Ｂ 股')):break
                    extra_tokens=list(re.finditer(DATE_VALUE,extra_line))
                    if len(extra_tokens)==1:combined.extend(extra_tokens)
                    elif extra_tokens:break
                ex_token=min(combined,key=lambda m:abs(m.start()-ex_col))
                if abs(ex_token.start()-ex_col)<=5:
                    parsed=tuple(parsed_date(m.groups()) for m in (record_token,ex_token,tokens[-1]))
                    if all(parsed):return parsed
        for candidate in lines[index + 1 : index + 7]:
            values = re.findall(DATE_VALUE, candidate)
            if len(values) >= 3:
                selected = (values[0], values[1], values[-1])
                parsed = tuple(parsed_date(value) for value in selected)
                if all(parsed):
                    return parsed[0], parsed[1], parsed[2]
    return None, None, None


def declaration_scope(compact: str) -> str:
    """Declared entitlements belong to this plan, not historical examples.

    Price-adjustment sections often reproduce distributions from many earlier
    years. Dates and effective market-reference formulas are still parsed from
    the complete announcement, but nominal cash/stock terms use the plan.
    """
    headings=list(re.finditer(r'[一二三四五六七八九十]+[、.．](?:本次(?:实施的)?)?(?:利润分配|权益分派|分红派息|分配)(?:及(?:资本公积金)?转增股本)?(?:方案|情况)',compact))
    for heading in sorted(headings,key=lambda h:('本次' not in h.group(),h.start())):
        rest=compact[heading.end():]
        next_heading=re.search(r'[一二三四五六七八九十]+[、.．]',rest)
        section=rest[:next_heading.start()] if next_heading else rest
        if re.search(r'每(?:10|一|1)?股',section):
            # An implementation section can omit the transfer ratio and refer
            # to the approved plan immediately before it. Reuse that section
            # only when the announcement explicitly confirms both unchanged
            # capital and consistency with the approved current plan.
            prefix=compact[:heading.start()]
            if re.search(r'(?:股本总[额数]|总股本)未发生变化',prefix) and re.search(r'本次实施的?分配方案.{0,70}一致',prefix):
                first=re.search(r'[一二三四五六七八九十]+[、.．]',prefix)
                if first:section+='。'+prefix[first.start():]
            return section
    # Some PDFs omit numbered plan headings. Never let an explicit historical
    # review/commitment-adjustment section supply missing current share terms.
    boundary=re.search(r'[一二三四五六七八九十]+[、.．](?:调整相关参数|相关参数调整|股东承诺)',compact)
    return compact[:boundary.start()] if boundary else compact


def clean_page_artifacts(text: str) -> str:
    lines=text.splitlines();headers={line.strip() for line in lines if line.strip().endswith('股份有限公司')
        and len(line.strip())<=50 and sum(other.strip()==line.strip() for other in lines)>1}
    return '\n'.join(line for line in lines if line.strip() not in headers and not re.fullmatch(
        r'\s*(?:\d{1,3}(?:\s*/\s*\d{1,3})?|第\s*\d+\s*页\s*共\s*\d+\s*页)\s*',line))


def parse_distribution(text: str) -> dict[str, object] | None:
    compact = normalize_text(clean_page_artifacts(text))
    differential = bool(re.search(r"差异化分红送转[:：]?是", compact))
    record_date = labeled_date(compact, ("股权登记日",))
    ex_labels = (
        "除权除息日", "除权除息息日", "除权日（除息日）",
        "除权（息）日", "除权（除息）日", "除权日", "除息日",
    )
    ex_date = labeled_date(compact, ex_labels)
    ex_date = ex_date or labeled_month_day(
        compact, ex_labels, record_date.year if record_date else None
    )
    pay_date = labeled_date(compact, ("现金红利发放日", "红利发放日", "红利发放日）", "红利发放日)"))
    pay_date = pay_date or narrative_pay_date(compact)
    table_record, table_ex, table_pay = table_dates(text)
    record_date = record_date or table_record
    ex_date = ex_date or table_ex
    pay_date = pay_date or table_pay
    ex_date_recovered_from_pay_date = False
    if (
        not ex_date and pay_date and record_date and pay_date > record_date
        and any(label in compact for label in ex_labels)
    ):
        # Some issuer PDFs contain a malformed ex-date token (for example
        # ``6月31日``) but repeat the valid payment date later. For cash-only
        # A-share distributions both dates coincide; retain the provenance and
        # downgrade quality instead of silently accepting the malformed token.
        ex_date = pay_date
        ex_date_recovered_from_pay_date = True
    invalid_pay_date=bool(pay_date and ex_date and pay_date<ex_date)
    if invalid_pay_date:
        # A typo in the source year must not create spendable cash before the
        # entitlement exists. Leave the payment unknown rather than guessing.
        pay_date=None
    declared=declaration_scope(compact)
    # The official front-page summary is also about this distribution. It is
    # a bounded second source when the main plan describes only total cash.
    first_heading=re.search(r'[一二三四五六七八九十]+[、.．]',compact)
    summary=compact[:first_heading.start()] if first_heading else ''
    declared_cash = declared_cash_amount(declared) or declared_cash_amount(summary)
    effective_cash = effective_cash_ratio(compact,declared_cash)
    cash = declared_cash if effective_cash is None else effective_cash
    stock = first_ratio(declared, (
        rf"每10股送(?:红股|股)?{NUMBER}股",
        rf"每10股派送红股{NUMBER}股",
    ))
    stock = stock or first_per_share(declared, (
        rf"每股(?:派)?送(?:红股)?{NUMBER}股",
    ))
    split = first_ratio(declared, (
        rf"每10股(?:以资本公积金)?转增(?:股|股份)?{NUMBER}股",
        rf"每10股资本公积(?:金)?转增{NUMBER}股",
    ))
    split = split or first_per_share(declared, (
        rf"每股(?:以资本公积金)?转增(?:股份)?{NUMBER}股",
    ))
    split = split or first_ratio(declared,(
        rf"每10股以资本公积(?:金)?转增股本{NUMBER}股",
    ))
    if not split and not stock:
        # Do not combine a summary's loose '送' wording for capital transfers
        # with an already parsed transfer and accidentally double the shares.
        if not re.search(r'不(?:进行|以)?(?:资本)?公积金?转增|不转增',declared):
            split=first_ratio(summary,(rf"每10股(?:以资本公积金)?转增(?:股|股份)?{NUMBER}股",))
        if not re.search(r'不(?:派)?送(?:红)?股',declared):
            stock=first_ratio(summary,(rf"每10股送(?:红股|股)?{NUMBER}股",))
    declared_split, declared_stock = split, stock
    effective_share_change = effective_share_ratio(compact, split + stock)
    if effective_share_change is not None and split + stock:
        scale = effective_share_change / (split + stock)
        split, stock = split * scale, stock * scale
        differential = differential or abs(scale-1)>1e-8
    if not ex_date or not any((cash, stock, split)):
        return None
    if split:
        action_type = "split"
    elif stock:
        action_type = "stock_dividend"
    else:
        action_type = "cash_dividend"
    return {
        "action_type": action_type,
        "record_date": record_date,
        "ex_date": ex_date,
        "pay_date": pay_date,
        "cash_dividend": cash or None,
        "stock_dividend_ratio": stock or None,
        "split_ratio": split or None,
        "declared_cash_dividend": declared_cash or None,
        "declared_split_ratio": declared_split or None,
        "declared_stock_dividend_ratio": declared_stock or None,
        "differential_distribution": differential,
        "effective_cash_ratio_parsed": bool(effective_cash),
        "effective_share_change_parsed": effective_share_change is not None,
        "ex_date_recovered_from_pay_date": ex_date_recovered_from_pay_date,
        "quality_status": "unverified" if (
            (differential and declared_cash and not effective_cash)
            or (differential and (declared_split or declared_stock) and effective_share_change is None)
            or ex_date_recovered_from_pay_date
            or invalid_pay_date
        ) else "verified",
    }


def is_explicit_non_a_share(stock_code: str | None, stock_name: str | None) -> bool:
    code = str(stock_code or "").strip()
    name = normalize_text(str(stock_name or "")).upper()
    return code.startswith(("200", "900")) or "B股" in name or "Ｂ" in name


def pdf_text(path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract CNINFO corporate actions from official PDFs.")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument(
        "--partition-key",
        default="default",
        help="Watermark partition. Historical backfills must use 'history'.",
    )
    args = parser.parse_args()
    until = dt.date.fromisoformat(args.until) if args.until else dt.datetime.now(TZ).date()
    since = (
        dt.date.fromisoformat(args.since)
        if args.since
        else until - dt.timedelta(days=max(1, args.lookback_days) - 1)
    )
    if since > until:
        parser.error("--since cannot be after --until")

    with stage_run(
        "finance_v3_morning",
        "corporate_actions_cninfo",
        expected_date=until,
        metadata={
            "since": since.isoformat(), "until": until.isoformat(),
            "partition_key": args.partition_key,
        },
    ) as run:
        with connect(autocommit=False) as conn:
            filings = conn.execute(
                """
                SELECT f.asset_id,f.stock_code,f.source_external_id,f.title,
                       f.announced_at,f.available_at,f.source_url,f.document_id,
                       f.metadata->>'stock_name' AS stock_name,df.file_path
                FROM company.filings f
                JOIN corpus.document_files df
                  ON df.document_id=f.document_id AND df.file_role='pdf'
                WHERE f.source_name='cninfo'
                  AND f.filing_type='corporate_action'
                  AND f.announced_at::date BETWEEN %s AND %s
                  AND f.title ~ '(权益分派|利润分配|分红派息).{0,30}实施(的)?公告'
                ORDER BY f.announced_at,f.source_external_id
                LIMIT %s
                """,
                (since, until, max(1, args.limit)),
            ).fetchall()
            parsed = 0
            excluded_non_a_share = 0
            scanned_pending_ocr = 0
            missing_pdf = 0
            mapping_errors = 0
            unsupported_format = 0
            errors: list[dict[str, str]] = []
            for filing in filings:
                path = Path(filing["file_path"])
                try:
                    if not path.exists() or not IMPLEMENTATION_TITLE_RE.search(filing["title"] or ""):
                        missing_pdf += 1
                        raise ValueError("official implementation PDF is unavailable")
                    if not filing["asset_id"]:
                        if is_explicit_non_a_share(filing["stock_code"], filing["stock_name"]):
                            excluded_non_a_share += 1
                            continue
                        mapping_errors += 1
                        raise ValueError("official stock code is not mapped in market.assets")
                    text = pdf_text(path)
                    if len(normalize_text(text)) < 300:
                        scanned_pending_ocr += 1
                        conn.execute(
                            """
                            INSERT INTO corpus.document_quarantine (
                              document_id,reason_code,reason_detail,source_name,
                              source_url,metadata
                            ) VALUES (%s,'scanned_pdf_pending_ocr',%s,'cninfo',%s,%s)
                            ON CONFLICT (document_id,reason_code,released_at) DO UPDATE SET
                              reason_detail=EXCLUDED.reason_detail,
                              source_url=EXCLUDED.source_url,
                              metadata=corpus.document_quarantine.metadata || EXCLUDED.metadata
                            """,
                            (
                                filing["document_id"],
                                "pdftotext produced insufficient machine-readable text",
                                filing["source_url"],
                                Jsonb({"pdf_path": str(path), "pipeline": "corporate_actions:v2"}),
                            ),
                        )
                        raise ValueError("scanned PDF is pending OCR")
                    action = parse_distribution(text)
                    if not action:
                        unsupported_format += 1
                        raise ValueError("distribution plan or ex-date was not parsed")
                    save_distribution(conn,filing,action,path)
                    parsed += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append({
                        "external_id": str(filing["source_external_id"]),
                        "error": f"{type(exc).__name__}: {exc}"[:400],
                    })
            conn.commit()

        eligible = len(filings) - excluded_non_a_share
        coverage = 100.0 * parsed / eligible if eligible else 100.0
        quality = "pass" if eligible and coverage >= 99 else "warn"
        record_quality(
            run["run_id"], "market.corporate_actions", "official_pdf_parse_coverage",
            status="pass" if quality == "pass" else "warn", severity="warning",
            partition_key=args.partition_key,
            observed_value=coverage, threshold_value=99, affected_rows=eligible - parsed,
            sample=errors[:20],
            details={
                "candidate_pdfs": len(filings), "eligible_pdfs": eligible,
                "parsed": parsed, "excluded_non_a_share": excluded_non_a_share,
                "scanned_pending_ocr": scanned_pending_ocr,
                "missing_pdf": missing_pdf, "mapping_errors": mapping_errors,
                "unsupported_format": unsupported_format,
            },
        )
        set_dataset_watermark(
            "market.corporate_actions", schema_version="v3", run_id=run["run_id"],
            partition_key=args.partition_key,
            expected_date=until,
            data_through=dt.datetime.combine(until, dt.time(23, 59), TZ),
            row_count=parsed, eligible_count=eligible, coverage_pct=coverage,
            quality_status=quality, source_version="cninfo_equity_distribution:v2",
            details={
                "since": since.isoformat(), "until": until.isoformat(),
                "excluded_non_a_share": excluded_non_a_share,
                "scanned_pending_ocr": scanned_pending_ocr,
                "missing_pdf": missing_pdf, "mapping_errors": mapping_errors,
                "unsupported_format": unsupported_format, "errors": errors[:20],
            },
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {
            f"market.corporate_actions:{args.partition_key}": until.isoformat()
        }
    print({"eligible_pdfs": eligible, "parsed": parsed, "errors": len(errors), "quality_status": quality})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
