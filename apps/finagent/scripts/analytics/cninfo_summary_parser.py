"""Read issuer-level financial summaries with explicit table scope and units.

Never infer a company's totals from the first matching number elsewhere in a PDF.
Ambiguous/wrapped numeric columns remain missing; they are not shifted left.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import re

VERSION = "cninfo_scoped_summary:v3.3"
UNITS = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000), "亿元": Decimal(100000000)}
METRICS = {
    "revenue": ("营业收入",),
    "total_operating_revenue": ("营业总收入",),
    "net_profit_parent": ("归属于上市公司股东的净利润", "归属于母公司股东的净利润"),
    "deducted_net_profit": ("归属于上市公司股东的扣除非", "扣除非经常性损益后的净利润"),
    "profit_before_tax": ("利润总额",),
    "operating_cash_flow": ("经营活动产生的现金流量净额", "经营活动产生的现金流量净"),
    "total_assets": ("总资产", "资产总额", "资产总计"),
    "equity_parent": ("归属于上市公司股东的净资产",),
    "eps_basic": ("基本每股收益",),
    "eps_diluted": ("稀释每股收益",),
    "roe_weighted": ("加权平均净资产收益率",),
    "rd_revenue_ratio": ("研发投入占营业收入的比例",),
}
NAMES = {code: labels[0] for code, labels in METRICS.items()}
NUMBER = re.compile(r"(?<![\w.,])(?:\([-−]?\d[\d,]*(?:\.\d+)?\)|[-−]?\d[\d,]*(?:\.\d+)?)(?![\w.,])")
HEADING = re.compile(r"^(?:(?:[一二三四五六七八九十]+|\d+)[、.．]|[（(][一二三四五六七八九十]+[）)]|\d+\.\d+)?(?:公司)?(?:主要会计数据(?:和财务指标)?|主要财务数据|主要财务指标)$")
SECTION = re.compile(r"^(?:[一二三四五六七八九十]+[、.]|\d+[、]|\d+\.\d+(?!\d))[^\d.,%]")


def compact(value):
    return re.sub(r"\s+", "", value)


def issuer_identity(text, stock_code):
    """Check explicit issuer codes before the financial summary, not mentions
    of subsidiaries or shareholders later in the report. A filing distributed
    by a parent can contain a listed subsidiary's own financial statements.
    """
    prefix = []
    for line in text[:40000].splitlines():
        if HEADING.fullmatch(compact(line)):
            break
        prefix.append(line)
    codes = set()
    for i,line in enumerate(prefix):
        # Only a code/name caption or the A-share row of the issuer's stock
        # table establishes identity. Definitions often quote other issuers'
        # "股票代码" inside parentheses; those are not identity declarations.
        stripped=line.strip()
        if re.match(r'^(?:证券|股票)(?:代码|简称)',stripped):
            codes.update(re.findall(r'(?:证券|股票)代码\s*[:：]?\s*(?:[ABＡＢ]股\s*)?([0368]\d{5}|920\d{3})(?!\d)',line))
        elif re.match(r'^(?:股票种类|股票类型|证券种类)',stripped) and ('代码' in stripped):
            for row in prefix[i+1:i+4]:
                if re.match(r'^\s*(?:A股|Ａ股|人民币普通股)',row):
                    codes.update(re.findall(r'(?<!\d)([0368]\d{5}|920\d{3})(?!\d)',row))
    codes = sorted(codes)
    return {'expected_stock_code': str(stock_code), 'explicit_issuer_codes': codes,
            'conflict': bool(codes and str(stock_code) not in codes)}


def decimal(raw):
    raw = raw.replace(",", "").replace("−", "-")
    if raw.startswith("("):
        raw = "-" + raw[1:-1]
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def summary_sections(text):
    lines = text.replace("\r", "").splitlines()
    for start, line in enumerate(lines):
        if not HEADING.fullmatch(compact(line)):
            continue
        end = min(len(lines), start + 170)
        for j in range(start + 1, end):
            candidate = compact(lines[j])
            if SECTION.match(candidate) and not HEADING.fullmatch(candidate):
                end = j
                break
        section = lines[start:end]
        # A section heading alone does not establish the meaning of its columns.
        header = compact("\n".join(section[:45]))
        if ("本报告期" not in header and "本期" not in header and "本年" not in header) or not any(x in header for x in ("上年同期", "上年度末", "上年末", "上期", "上年")):
            continue
        yield start, section


def parse_summary(text: str, report_end: dt.date | None = None):
    found = {}
    conflicts = set()
    warnings = []
    aliases = sorted([(label, code) for code, labels in METRICS.items() for label in labels], reverse=True, key=lambda p: len(p[0]))
    for section_start, lines in summary_sections(text):
        context = compact("\n".join(lines))
        global_units = list(re.finditer(r"单位[:：](亿元|万元|千元|元)", context[:1400]))
        global_unit = global_units[0].group(1) if global_units else None
        pending = ""
        for i, line in enumerate(lines):
            # Corrupt embedded OCR can turn 24,899,581 into 24,899,58I.
            # Never parse a valid-looking prefix/suffix of such a cell.
            if re.search(r'\d[\d,.]*[Il!|]|[Il|][\d,]', line):
                warnings.append({'reason':'corrupt_numeric_glyphs','line':section_start+i+1})
                pending = ''
                continue
            tokens = list(NUMBER.finditer(line))
            prefix = compact(line[:tokens[0].start()]) if tokens else compact(line)
            label_context = prefix
            if len(tokens)>=2:
                before=[];after=[]
                for nearby in reversed(lines[max(0,i-3):i]):
                    if not nearby.strip(): continue
                    if re.fullmatch(r'注\s*\d+',nearby.strip()): continue
                    if re.search(r'\d',nearby): break
                    before.insert(0,nearby)
                    if len(before)==2: break
                for nearby in lines[i+1:i+4]:
                    if not nearby.strip(): continue
                    if re.fullmatch(r'注\s*\d+',nearby.strip()): continue
                    if re.search(r'\d',nearby): break
                    after.append(nearby)
                    if len(after)==2: break
                # PDF table labels often wrap above and below the numeric row.
                # Join label fragments only; never move numeric cells across rows.
                joined=compact(''.join(before)+prefix+''.join(after))
                before_label = next((code for label,code in aliases if compact(''.join(before)).startswith(label)), None)
                after_label = next((code for label,code in aliases if compact(''.join(after)).startswith(label)), None)
                if not prefix and before_label and after_label and before_label != after_label:
                    warnings.append({'reason':'ambiguous_detached_row_label','line':section_start+i+1})
                    pending = ''
                    continue
                if not any(prefix.startswith(label) for label,_ in aliases):
                    label_context=joined
            match = next(((label, code) for label, code in aliases if label_context.startswith(label)), None)
            if not match and not prefix and pending and not re.search(r'\d',pending):
                match = next(((label, code) for label, code in aliases if compact(pending).startswith(label)), None)
            if not match:
                pending = line if not tokens else ""
                continue
            label, code = match
            if code in conflicts:
                continue
            if any(word in prefix[len(label):] for word in ('增长','增减','占比','比重','构成')):
                pending = ''
                continue
            if len(tokens) < 2:
                pending = line if not re.search(r'\d',line) else ''
                continue
            evidence = '\n'.join(lines[max(0,i-3):min(len(lines),i+4)]).strip()
            restated=False
            if len(tokens)==4:
                headers=[compact(s) for s in lines[max(0,i-35):i] if '调整前' in s]
                # Exactly one original comparative, one restated comparative,
                # then growth on the restated basis. Other layouts remain ambiguous.
                if headers and re.fullmatch(r'调整前调整后调整后',headers[-1]):
                    tokens=[tokens[0],tokens[2],tokens[3]]
                    restated=True
            # Percent suffixes must not take the place of a missing monetary column.
            if code not in ("roe_weighted", "rd_revenue_ratio") and any(line[t.end():].lstrip().startswith("%") for t in tokens[:2]):
                warnings.append({"metric_code":code,"reason":"missing_or_wrapped_value_column","line":section_start+i+1})
                pending = ""
                continue
            if len(tokens) > 3:
                warnings.append({"metric_code":code,"reason":"ambiguous_restatement_or_quarter_columns","line":section_start+i+1})
                pending = ""
                continue
            source_unit_match = re.search(r"[（(](亿元|万元|千元|元)[）)]", label_context) or re.search(r"[（(](亿元|万元|千元|元)[）)]", compact(line))
            source_unit = source_unit_match.group(1) if source_unit_match else global_unit
            unit = "CNY/share" if code.startswith("eps_") else "%" if code in ("roe_weighted", "rd_revenue_ratio") else "CNY"
            if unit == "CNY" and not source_unit:
                warnings.append({"metric_code":code,"reason":"missing_unit","line":section_start+i+1})
                pending = ""
                continue
            values = [decimal(t.group()) for t in tokens]
            if any(v is None for v in values):
                continue
            multiplier = UNITS[source_unit] if unit == "CNY" else Decimal(1)
            value, previous = values[0] * multiplier, values[1] * multiplier
            yoy = None
            header = compact("\n".join(lines[max(0,i-22):i]))
            growth_header = any(word in header for word in ("增减", "增长", "同比"))
            if len(values) == 3 and growth_header and unit != "%":
                reported = values[2]
                if previous != 0:
                    calculated = (value - previous) / abs(previous) * 100
                    # Source amounts and EPS can be rounded; compare with the
                    # maximum error implied by their displayed precision.
                    quantum = max(Decimal(1).scaleb(values[k].as_tuple().exponent) for k in (0,1)) * multiplier
                    tolerance = max(Decimal("0.11"), quantum / abs(previous) * (1 + abs(value / previous)) * 100)
                    if abs(calculated - reported) <= tolerance:
                        yoy = reported
                    else:
                        warnings.append({"metric_code":code,"reason":"reported_growth_does_not_reconcile","line":section_start+i+1})
                else:
                    warnings.append({"metric_code":code,"reason":"zero_comparative_growth","line":section_start+i+1})
            record = {"metric_code":code,"metric_name":NAMES[code],"value":value,"yoy":yoy,"unit":unit,
                      "source_unit":source_unit or ("元/股" if unit == "CNY/share" else "%"),
                      "source_text":evidence[:1800],"statement_section":"issuer_summary",
                      "comparative":False,"parser":VERSION,"prior_value":str(previous),
                      "comparative_basis":"restated" if restated else "reported",
                      "source_line":section_start+i+1,"source_page":text[:text.find(line)].count("\f")+1 if line in text else None,
                      "field_checks":{"scope":"issuer_summary","explicit_unit":True,"period_columns":True,"growth_reconciled":yoy is not None}}
            if report_end is not None:
                point = code in ("total_assets", "equity_parent")
                record.update(period_end=report_end,period_start=None if point else dt.date(report_end.year,1,1),
                              period_type="point_in_time" if point else "annual" if report_end.month==12 else "year_to_date")
            if code in found and found[code]["value"] != value:
                warnings.append({"metric_code":code,"reason":"conflicting_summary_values","line":section_start+i+1})
                found.pop(code, None)
                conflicts.add(code)
            else:
                found[code] = record
            pending = ""
    return list(found.values()), warnings
