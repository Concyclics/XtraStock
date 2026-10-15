# -*- coding: utf-8 -*-
"""Rule-based keyword and event extraction for finance documents."""
from __future__ import annotations

import argparse
import hashlib
import re
from dataclasses import dataclass

from common import parse_source_types
from company_evidence_scope import scope_evidence,VERSION as ATTRIBUTION_VERSION
from cninfo_summary_parser import issuer_identity


@dataclass(frozen=True)
class EventRule:
    event_type: str
    polarity: int
    impact_score: float
    keywords: tuple[str, ...]


RULES = [
    EventRule("policy_support", 1, 0.8, ("政策支持", "政策利好", "支持", "鼓励", "行动计划", "实施方案", "指导意见", "国务院", "发改委", "工信部")),
    EventRule("government_investment", 1, 0.9, ("政府投资", "财政资金", "专项债", "特别国债", "中央预算内投资", "重大项目", "项目开工", "基建投资")),
    EventRule("subsidy", 1, 0.7, ("补贴", "贴息", "以旧换新", "税收优惠", "减免", "奖补")),
    EventRule("demand_growth", 1, 0.6, ("需求增长", "订单增长", "景气上行", "旺季", "渗透率提升", "出口增长", "销量增长")),
    EventRule("price_hike", 1, 0.6, ("涨价", "价格上涨", "提价", "调价", "价差扩大", "价格中枢上移")),
    EventRule("order_contract", 1, 0.7, ("中标", "签订合同", "大单", "订单", "定点", "客户突破", "批量供货")),
    EventRule("earnings_positive", 1, 0.7, ("业绩预增", "超预期", "利润增长", "盈利改善", "扭亏", "毛利率提升")),
    EventRule("regulation_risk", -1, 0.7, ("监管趋严", "处罚", "调查", "限制", "禁令", "整改", "反垄断", "集采降价")),
    EventRule("demand_decline", -1, 0.6, ("需求下滑", "订单减少", "销量下降", "景气下行", "库存压力", "去库存")),
    EventRule("price_cut", -1, 0.6, ("降价", "价格下跌", "价格承压", "价差收窄", "价格战")),
    EventRule("external_risk", -1, 0.6, ("出口管制", "制裁", "关税", "地缘冲突", "贸易摩擦", "汇率波动")),
]

INDUSTRY_KEYWORDS = (
    "半导体", "人工智能", "算力", "新能源", "光伏", "风电", "储能", "电池", "汽车", "机器人", "医药",
    "创新药", "医疗器械", "地产", "基建", "银行", "证券", "保险", "军工", "农业", "食品饮料", "消费",
    "化工", "有色", "煤炭", "钢铁", "电力", "通信", "软件", "游戏", "传媒", "低空经济",
)

LABELED_STOCK_CODE_RE = re.compile(
    r"(?:股票代码|证券代码|证券简称及代码|股票简称及代码|代码)\s*[:：]?\s*((?:[0368]\d{5}|920\d{3}))(?!\d)"
)


class CompanyResolver:
    def __init__(self, rows: list[dict]):
        self.by_name = {
            str(row["name"]).strip(): str(row["symbol"]).strip()
            for row in rows
            if row.get("name") and len(str(row["name"]).strip()) >= 3
        }
        self.names_by_code = {code: name for name, code in self.by_name.items()}
        names = sorted(self.by_name, key=len, reverse=True)
        self.pattern = re.compile("|".join(re.escape(name) for name in names)) if names else None

    def resolve(self, text: str, limit: int = 20) -> tuple[set[str], set[str]]:
        if not self.pattern:
            return set(), set()
        names: set[str] = set()
        codes: set[str] = set()
        for match in self.pattern.finditer(text):
            name = match.group(0)
            names.add(name)
            codes.add(self.by_name[name])
            if len(codes) >= limit:
                break
        return codes, names


def make_hash(document_id: str, event_type: str, evidence: str, method: str) -> str:
    text = f"{document_id}|{event_type}|{evidence[:240]}|{method}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find_evidence(text: str, keyword: str, window: int = 80) -> str:
    idx = text.find(keyword)
    if idx < 0:
        return keyword
    start = max(0, idx - window)
    end = min(len(text), idx + len(keyword) + window)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def extract(row: dict, method: str, company_resolver: CompanyResolver | None = None) -> tuple[list[dict], list[tuple[str, float]]]:
    text = "\n".join(
        x for x in (row.get("title"), row.get("content_text"), row.get("content_md"), row.get("raw_text")) if x
    )
    if not text:
        return [], []

    document_stock_codes = set(LABELED_STOCK_CODE_RE.findall(text))
    report_stock_code = None
    identity = None
    if row.get("stock_code") and str(row.get("report_type") or "").lower() not in {"industry", "macro", "strategy"}:
        report_stock_code = row["stock_code"]
        if row.get('source_type') == 'announcement':
            identity=issuer_identity(text,report_stock_code)
            if identity['conflict']:
                codes=identity['explicit_issuer_codes']
                report_stock_code=codes[0] if len(codes)==1 else None
        document_stock_codes.add(report_stock_code)
        document_stock_codes.discard(None)

    industries = {kw for kw in INDUSTRY_KEYWORDS if kw in text}
    if row.get("industry_name"):
        industries.add(row["industry_name"])

    keyword_weights: dict[str, float] = {}
    events = []
    for rule in RULES:
        hits = [kw for kw in rule.keywords if kw in text]
        if not hits:
            continue
        for kw in hits:
            keyword_weights[kw] = max(keyword_weights.get(kw, 0.0), rule.impact_score)
        evidence = find_evidence(text, hits[0])
        entity_context = find_evidence(text, hits[0], window=120)
        event_stock_codes = set(LABELED_STOCK_CODE_RE.findall(entity_context))
        event_company_names: set[str] = set()
        if company_resolver is not None:
            resolved_codes, event_company_names = company_resolver.resolve(entity_context)
            event_stock_codes.update(resolved_codes)
        if report_stock_code:
            event_stock_codes.add(report_stock_code)
        # A report can discuss many companies. Bind each event to its own
        # clause before assigning the whole report's issuer to the event.
        names_by_code = company_resolver.names_by_code if company_resolver else {}
        event_stock_codes = {code for code in event_stock_codes if scope_evidence(
            entity_context,code,names_by_code.get(code,row.get('stock_name') if code==report_stock_code else ''),
            hits,document_stock_code=report_stock_code)['supported']}
        event_company_names = {name for name in event_company_names
            if company_resolver and company_resolver.by_name.get(name) in event_stock_codes}
        event_industries = {kw for kw in INDUSTRY_KEYWORDS if kw in evidence}
        if row.get("industry_name"):
            event_industries.add(row["industry_name"])
        event = {
            "document_id": row["document_id"],
            "event_date": row.get("event_date"),
            "event_type": rule.event_type,
            "polarity": rule.polarity,
            "impact_score": rule.impact_score,
            "confidence": min(0.95, 0.45 + 0.12 * len(hits)),
            "entity_texts": sorted(event_industries | event_stock_codes | event_company_names),
            "stock_codes": sorted(event_stock_codes),
            "industries": sorted(event_industries),
            "keywords": hits,
            "summary": f"规则命中：{rule.event_type} / {', '.join(hits[:5])}",
            "evidence": evidence,
            "extraction_method": method,
            "event_hash": make_hash(row["document_id"], rule.event_type, evidence, method),
            "metadata": {"matched_keywords": hits, "source_type": row.get("source_type"), "source_name": row.get("source_name"),"entity_attribution":ATTRIBUTION_VERSION,"issuer_identity":identity},
        }
        events.append(event)

    for industry in industries:
        keyword_weights[industry] = max(keyword_weights.get(industry, 0.0), 0.5)
    for code in document_stock_codes:
        keyword_weights[code] = max(keyword_weights.get(code, 0.0), 0.5)
    return events, sorted(keyword_weights.items(), key=lambda x: (-x[1], x[0]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-types", default="research_report,peoples_daily,news,announcement")
    parser.add_argument("--since", help="Inclusive published/report date, YYYY-MM-DD.")
    parser.add_argument("--until", help="Inclusive published/report date, YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--force", action="store_true")
    parser.add_argument('--drain', action='store_true', help='Continue bounded batches until this date scope is exhausted.')
    parser.add_argument('--max-batches', type=int, default=100)
    parser.add_argument('--document-id', action='append', help='Bounded repair of explicitly selected document UUIDs.')
    parser.add_argument("--method", default="rules:v2")
    parser.add_argument("--logic-version", default="entity_clause:v5")
    parser.add_argument('--include-updated-announcements-hours',type=int,default=48,
        help='Also process older official announcements whose bodies were recently backfilled.')
    parser.add_argument('--batch-size', type=int, default=10,
        help='Bounded transaction size (1..50); contention falls back to individual retries.')
    parser.add_argument('--report-json', help='Optional local JSON summary; failures produce nonzero exit.')
    args = parser.parse_args()
    from document_event_rule_store import run
    if args.drain and args.force:
        parser.error('--drain cannot be combined with --force')
    for batch in range(max(1,args.max_batches) if args.drain else 1):
        result=run(args, parse_source_types(args.source_types), extract, CompanyResolver,
                   RULES, INDUSTRY_KEYWORDS, ATTRIBUTION_VERSION)
        if result or not args.drain or args.last_candidate_count < args.limit:
            return result
    print('incomplete: candidate drain batch budget reached', flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
