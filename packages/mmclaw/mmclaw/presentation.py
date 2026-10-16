"""Deterministic user-facing presentation, applied after evidence validation.

Internal evidence IDs, snapshots and validation records remain unchanged. This
module performs no retrieval or model calls and never adds financial claims.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from .reader_language import strip_boilerplate


UUID = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{8,12}"
IDENTIFIER = re.compile(rf"(?<![A-Za-z0-9])(?:{UUID}(?::E\d{{4}})?|[0-9a-fA-F]{{32}}:E\d{{4}}|[0-9a-fA-F]{{32,64}})(?![A-Za-z0-9])")
SHORT_REF = re.compile(r"\b(?:DOC|DQ|MKT|FAC|KG|FIN|EVT|REP|REPORT|NEWS|POOL|FOCUS|CAND|VAL|HOT|COVER)\d{4}\b")
SOURCE_NAMES = {
    "cninfo_official": "巨潮资讯（公司公告）", "cninfo": "巨潮资讯",
    "xinhua_daily": "新华每日电讯", "peoples_daily": "人民日报",
    "eastmoney_finance": "东方财富财经", "eastmoney_stock": "东方财富股票",
    "factor.evaluations": "因子历史检验", "eastmoney": "东方财富", "sina": "新浪财经", "system:clock": "北京时间",
}
DATASET_NAMES = {
    "agent.daily_data_gate": "整体数据可用性", "market.trading_calendar": "交易日历",
    "market.canonical_bars_daily_v2": "日行情", "market.asset_lifecycle_versions": "证券基本资料",
    "market.corporate_actions": "分红与股本变动", "market.adjustment_factors_daily": "复权数据",
    "company.filings": "公司公告", "company.financial_facts": "财务数据",
    "corpus.document_quality": "资料质量", "corpus.document_embeddings": "资料检索覆盖",
    "factor.daily_scores": "量化研究信号", "kg.temporal_graph": "产业关联资料",
}
TERMS = {
    **DATASET_NAMES, **SOURCE_NAMES,
    "same_day_ai_hardware_reports": "当日相关研报", "same_day_news_keywords": "当日新闻主题",
    "same_day_news": "当日新闻", "same_day_events": "当日事件",
    "latest_available_*": "最近可用资料", "decision_cutoff": "资料截止时间",
    "expected_market_date": "行情基准日", "lifecycle_status": "使用范围",
    "quality_status": "数据可靠性", "validation_status": "核验结果",
    "source_document_id": "原始文档编号", "document_id": "原文出处", "evidence_id": "来源编号",
    "score_date": "信号日期", "available_at": "公开时间", "as_of_date": "数据日期",
    "period_end": "报告期", "data_through": "数据截至日期", "coverage_pct": "覆盖率",
    "hard_gate_passed": "资料是否满足分析条件", "temporal graph": "产业关联资料",
    "model_signal": "研究信号", "graph_association": "产业关联", "document_claim": "资料记载",
    "data_quality": "数据可靠性", "report_claim": "研报观点", "observed_fact": "已披露事实",
    "extracted_claim": "资料提取线索", "model_inference": "模型推断",
    "actual": "已披露实际数据", "forecast": "预测值", "consensus": "机构一致预期",
    "shadow": "研究阶段", "candidate": "待验证信号", "degraded": "数据受限",
    "unverified": "尚未充分核验", "verified": "已核验", "incomplete": "资料尚不完整",
    "stale": "数据尚未更新", "rejected": "已排除", "conflict": "来源存在分歧",
    "warn": "存在数据限制", "fail": "未通过核验", "blocked": "暂不满足分析条件",
    "pending": "尚未完成", "complete": "已完成", "published": "已发布",
    "missing": "缺少资料", "failed": "未通过核验", "pass": "已通过核验",
    "quarantined": "暂缓发布", "percentile": "样本中相对位置", "zscore": "标准化分数",
    "z-score": "标准化分数", "production": "已正式启用",
    "research_only": "研究阶段", "prediction_eligible=false": "尚不具备预测资格",
    "prediction_eligible=true": "已通过预测资格审查", "prediction_eligible": "是否具备预测资格",
    "FORECASTS": "研报预测关联", "raw": "原始成交价格口径",
    "PRODUCES": "产品关联", "USES_TECHNOLOGY": "技术关联",
    "数据库事实": "已收录资料", "生命周期": "使用范围", "证据台账": "本次资料",
    "本证据账本": "本次资料", "证据账本": "本次资料",
    "本证据台账": "本次资料", "本证据账": "本次资料",
    "证据账": "本次资料", "本台账": "本次资料", "硬门禁阻断项": "当前缺少或未更新的资料",
    "数据硬门禁未通过": "当前资料尚不足以支持可靠分析",
}
TERM_RE = re.compile("|".join(
    rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])" if re.search(r"[A-Za-z]", key) else re.escape(key)
    for key in sorted(TERMS, key=len, reverse=True)
))


def _readable(text):
    return TERM_RE.sub(lambda m: TERMS[m.group(0)], str(text))


def source_catalog(data):
    """Index existing evidence/snapshot metadata; never guess truncated IDs."""
    catalog, document_aliases = {}, []

    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            if value.get("evidence_id"):
                catalog[str(value["evidence_id"])] = value
            if value.get("document_id") and (value.get("title") or value.get("report_title") or value.get("source_name")):
                catalog.setdefault(str(value["document_id"]), {"claim_type": "document_claim", "value": value})
            for key, kind in (("event_id", "document_claim"), ("relation_version_id", "graph_association")):
                if value.get(key):
                    catalog.setdefault(str(value[key]), {"claim_type": kind, "value": value})
                    if value.get("document_id"):
                        document_aliases.append((str(value[key]), str(value["document_id"])))
            for child in value.values():
                if isinstance(child, (list, dict)):
                    visit(child)

    visit(data)
    for alias, document in document_aliases:
        if document in catalog:
            catalog[alias] = catalog[document]
    return catalog


def _clean_label(text):
    text = re.sub(r"<[^>]+>", "", html.unescape(str(text or "")))
    text = IDENTIFIER.sub("", text)
    text = SHORT_REF.sub("", text)
    text = re.sub(r"[\[\]`*]", "", text)
    return " ".join(_readable(text).split())


def _source_label(item):
    value = item.get("value") or {}
    value = value if isinstance(value, dict) else {}
    kind = item.get("claim_type")
    # Clock, lookup and quality-control IDs are not useful reader citations.
    if kind == "data_quality":
        return None
    if kind == "calculation" and item.get("metric_id") not in {"finance_market_screen", "market_coverage", "valuation_screen", "sector_research_attention"} and not str(item.get("metric_id") or "").startswith("period_return_pct_"):
        return None
    name = value.get("source_system") or value.get("institution") or item.get("source_id") or value.get("source_name") or ""
    name = str(name)
    for key, label in SOURCE_NAMES.items():
        if name == key or name.startswith(key + ":"):
            name = label
            break
    if not name or IDENTIFIER.search(name) or "_" in name or ":" in name:
        name = {"actual": "公司财务披露", "forecast": "机构预测", "consensus": "机构一致预期",
                "market": "行情资料", "graph_association": "产业关联资料"}.get(kind, "参考资料")
    if kind == "model_signal":
        name = "量化研究记录"
    if kind == "calculation":
        name = "行情与估值资料" if item.get("metric_id") == "finance_market_screen" else "行情区间计算"
        name = {"market_coverage":"A股数据覆盖统计", "valuation_screen":"机构盈利预测与估值筛选",
                "sector_research_attention":"行业研报关注统计"}.get(item.get("metric_id"),name)
    date = item.get("period_end") or item.get("as_of_date") or value.get("period_end") or value.get("doc_date") or value.get("report_date") or value.get("event_date") or value.get("published_date") or value.get("dt") or value.get("valid_from")
    title = _clean_label(value.get("title") or value.get("report_title"))[:110]
    if item.get('metric_id') in {'valuation_screen','sector_research_attention'}:
        title = _clean_label(value.get('name') or value.get('industry'))[:80]
    if item.get('metric_id') == 'research_focus':
        name = '研究关注'
        title = _clean_label(value.get('label'))[:80]
    elif item.get('metric_id') == 'learning_state':
        name = '模型历史检验与前瞻复盘'
    elif str(item.get('metric_id') or '').startswith('candidate_pool'):
        name = '自动候选池与周期复核记录'
        title = _clean_label(value.get('name'))[:80]
    elif item.get('metric_id') == 'frozen_research_watchlist':
        name = '公司研究档案'
        title = _clean_label(value.get('name') or (value.get('research') or {}).get('stock_name'))[:80]
    elif item.get('metric_id') == 'daily_value_plan':
        name = '候选计划复核记录'
        title = _clean_label(value.get('name'))[:80]
    if not title and kind == "actual":
        title = "公司财务披露"
    label = _clean_label(name) + (f"《{title}》" if title else "")
    if value.get("event_id") and not title and name == "参考资料":
        label = "事件提取记录（原文出处待核对）"
    if kind == "graph_association":
        publisher = value.get("publisher") or "产业关联资料"
        label = _clean_label(SOURCE_NAMES.get(publisher, publisher))
        if title:
            label += f"《{title}》"
        date = value.get("published_date") or date
        if value.get("source_name") and value.get("target_name"):
            label += f"：{_clean_label(value['source_name'])} → {_clean_label(value['target_name'])}"
        if not (item.get("source_document_id") or value.get("document_id")):
            label += "（原文出处未提供）"
    if date:
        label += f"，{'报告期' if kind == 'actual' else ''}{str(date)[:10]}"
    if item.get("metric_id") == "finance_market_screen":
        valuation_date = value.get("valuation_date") or (value.get("source_dates") or {}).get("valuation_date")
        if valuation_date and str(valuation_date)[:10] != str(date)[:10]:
            label += f"（估值数据日期 {str(valuation_date)[:10]}）"
    # Different metrics from one document share one short reader reference.
    key = item.get("source_document_id") or value.get("source_document_id") or value.get("document_id") or label
    if kind == "actual":
        key = f"{key}:{date}"  # One filing can contain different reporting periods.
    url = str(value.get("url") or "")
    try:
        usable_url = urlsplit(url).scheme in {"http", "https"}
    except ValueError:
        usable_url = False
    if usable_url and not re.search(r"[\s<>\[\]()]", url):
        label = f"[{label}]({url})"
    return str(key), label


def render_finance_text(text, evidence=None, *, catalog=None):
    """Replace internal citations with compact, source-backed reader references."""
    text = strip_boilerplate(str(text or ""))
    catalog = catalog if catalog is not None else source_catalog(evidence or {})
    # Legacy degraded reports embedded the full provider exception here. The
    # original stays in report_audited.md; readers need the operational impact.
    text = re.sub(r"模型生成降级原因[：:][^\n]*",
                  "日报正文未能完成生成或通过核验，本期暂不提供分析结论。", text)
    # URLs may legitimately contain hashes/IDs. Preserve link targets verbatim.
    urls = []
    def protect(match):
        urls.append(match.group(0))
        return f"\ue000URL{len(urls)-1}\ue001"
    text = re.sub(r"https?://[^\s<>`）)\]]+", protect, text)
    text = re.sub(r"\b(?:request_id|turn_id|run_id|report_run_id|snapshot_sha256|prompt_hash|tool_schema_hash|sha256)\b\s*[:：=]\s*[`\"']?[A-Za-z0-9:_-]+[`\"']?", "", text)
    text = re.sub(r"(?:快照哈希|内部编号)\s*[:：=]\s*[`\"']?[A-Za-z0-9:_-]+[`\"']?", "", text)
    text = re.sub(r"追踪编号[：:]\s*[0-9a-fA-F]{6,64}", "", text)
    references, source_numbers = [], {}

    def reference(identifier):
        item = catalog.get(identifier)
        if not item:
            # With a real source snapshot, a broken citation remains visible as
            # a provenance gap. Never fuzzy-match a truncated ID to a document.
            return "（出处待核对）" if catalog else ""
        source = _source_label(item)
        if not source:
            return ""
        key, label = source
        if key not in source_numbers:
            source_numbers[key] = len(references) + 1
            references.append(label)
        return f"[来源{source_numbers[key]}]"

    def citation_group(match):
        ids = IDENTIFIER.findall(match.group(0)) + SHORT_REF.findall(match.group(0))
        refs = list(dict.fromkeys(reference(identifier) for identifier in ids))
        return "".join(refs)

    # Handle grouped V3 report references before replacing standalone UUIDs.
    text = re.sub(r"\[(?:evidence\s*:\s*)?[^\]\n]*(?:\b(?:DOC|DQ|MKT|FAC|KG|FIN|EVT|REP|REPORT|NEWS|POOL|FOCUS|CAND|VAL|HOT|COVER)\d{4}|:E\d{4})[^\]\n]*\]", citation_group, text)
    text = IDENTIFIER.sub(lambda m: reference(m.group(0)), text)
    text = re.sub(r"(?<![A-Za-z0-9:])E\d{4}\b", "（出处待核对）", text)
    text = re.sub(r"`(\[来源\d+\])`", r"\1", text)
    # Short report IDs appear outside brackets in some degraded reports too.
    text = SHORT_REF.sub(lambda m: reference(m.group(0)), text)
    text = re.sub(r"(?:\[来源\d+\][、,， ]*){2,}",
                  lambda m: "".join(dict.fromkeys(re.findall(r"\[来源\d+\]", m.group(0)))), text)
    text = _readable(text)
    text = text.replace("巨潮资讯（公司公告），即巨潮资讯（公司公告）", "巨潮资讯（公司公告）")
    text = text.replace("研究报告中的研报观点", "研报观点")
    text = text.replace("使用范围为研究阶段", "研究阶段")
    text = text.replace("质量状态为", "核验状态为")
    # Remove empty code spans first, so adjacent removed citations do not leave
    # stray delimiters and a second render yields exactly the same text.
    text = text.replace("``", "")
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"(?:相关文档|证据|文档|事件)[：:]\s*(?:[、,，；; ]*)[。.]", "", text)
    text = re.sub(r"证据[：:]文档\s*", "来源：", text)
    text = re.sub(r"(?:来源|证据)[：:]\s*(\[来源\d+\])", r"\1", text)
    text = re.sub(r"\[\s*\]|\(\s*\)|（\s*）", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if references:
        text += "\n\n参考来源\n" + "\n".join(f"[{index}] {label}" for index, label in enumerate(references, 1))
    for index, url in enumerate(urls):
        text = text.replace(f"\ue000URL{index}\ue001", url)
    return text


def render_report_file(path):
    """Render old reports on read; canonical files and snapshots stay intact."""
    path = Path(path)
    catalog_path = path.with_suffix(".sources.json")
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    else:
        pack = path.parent.parent / "runs" / path.stem / "data_pack.json"
        catalog = source_catalog(json.loads(pack.read_text(encoding="utf-8"))) if pack.exists() else {}
    return render_finance_text(path.read_text(encoding="utf-8"), catalog=catalog)


def publish_readable_report(draft_path, daily_path, data_pack_path):
    draft_path, daily_path, data_pack_path = map(Path, (draft_path, daily_path, data_pack_path))
    text = draft_path.read_text(encoding="utf-8")
    catalog = source_catalog(json.loads(data_pack_path.read_text(encoding="utf-8"))) if data_pack_path.exists() else {}
    # Keep the validated original next to the run's snapshot, before publishing.
    (draft_path.parent / "report_audited.md").write_text(text, encoding="utf-8")
    public = render_finance_text(text, catalog=catalog) + "\n"
    temporary = daily_path.with_suffix(".readable.tmp")
    temporary.write_text(public, encoding="utf-8")
    temporary.replace(daily_path)
    daily_path.with_suffix(".sources.json").write_text(json.dumps(catalog, ensure_ascii=False, default=str), encoding="utf-8")
    return public
