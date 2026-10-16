#!/usr/bin/env python3
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import textwrap
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from common import ROOT, TZ, connect, today
from data_freshness_v3 import freshness_issue
from openai_compatible_client import complete as openai_compatible_complete
from daily_llm_payload import compact_v3_snapshot, inline_json
from user_presentation import publish_readable_report, READER_STYLE_PROMPT, strip_boilerplate
from report_capabilities import analysis_capabilities, usable_evidence, source_digest, has_actionable_recommendation, claim_evidence_error, evidence_numeric_projection


ANALYSIS_DIR = ROOT / "analysis/AI_Hardware"
RUNS_DIR = ANALYSIS_DIR / "runs_v3"
SHADOW_DIR = ANALYSIS_DIR / "daily_v3_shadow"
DAILY_DIR = ANALYSIS_DIR / "daily"
REQUIRED_DATASETS = (
    "agent.daily_data_gate",
    "market.trading_calendar",
    "market.canonical_bars_daily_v2",
    "market.asset_lifecycle_versions",
    "market.corporate_actions",
    "market.adjustment_factors_daily",
    "company.filings",
    "company.financial_facts",
    "corpus.document_quality",
    "corpus.document_embeddings",
    "factor.daily_scores",
    "kg.temporal_graph",
)
SECTIONS = (
    "省流版",
    "今日核心判断",
    "今日新增新闻与催化",
    "主题热度变化",
    "研究关注更新",
    "长期候选池更新",
    "细分产业链观察",
    "个股观察池",
    "关键证据",
    "今日投资建议",
    "明日跟踪点",
    "数据质量与限制",
)
SECTION_ALIASES = {
    "数据质量与时效": "数据质量与限制",
    "数据范围与风险": "数据质量与限制",
    "模型信号边界": "数据质量与限制",
    "行业与券商观点": "细分产业链观察",
    "供给约束与风险": "细分产业链观察",
    "需求与应用场景": "细分产业链观察",
    "具身智能与新硬件": "细分产业链观察",
    "政策与产业化": "今日新增新闻与催化",
    "市场与个股观察": "个股观察池",
}
CLAIM_TYPES = {
    "actual", "forecast", "market", "model_signal", "document_claim",
    "graph_association", "calculation", "data_quality", "interpretation",
}
AI_HARDWARE_RE = (
    r"AI|人工智能|算力|GPU|ASIC|CPO|光模块|光芯片|光通信|HBM|DDR|存储|"
    r"PCB|HDI|封装|半导体|芯片|服务器|数据中心|液冷|电源|光刻|刻蚀|"
    r"靶材|光刻胶|六氟化钨|MPO|MLCC|玻璃基板|TGV|铜缆|高速连接|交换机|硅光"
)
NUMERIC_RE = re.compile(r"(?<![A-Za-z0-9_-])-?\d+(?:\.\d+)?%?")
STOCK_RE = re.compile(r"(?<!\d)[0368]\d{5}(?!\d)")
ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
DOT_DATE_RE = re.compile(r"(?<!\d)(\d{4})\.(\d{1,2})\.(\d{1,2})(?!\d)")
CN_DATE_RE = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
CN_YEAR_MONTH_RE = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月")
CN_MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
HALF_YEAR_RANGE_RE = re.compile(r"(?<!\d)(\d{4})\s*年\s*(1\s*[-–至]\s*6|7\s*[-–至]\s*12)\s*月")
PERIOD_RE = re.compile(r"(?<!\d)(\d{4})\s*(?:年\s*)?(?:([上下])半年|[Hh]([12])|第?([一二三四1234])季度|[Qq]([1-4]))")
DECLINE_PERCENT_RE = re.compile(r"((?:下降|下滑|下跌|减少|降低)\s*)(\d+(?:\.\d+)?%)")
ISO_DATETIME_RE = re.compile(
    r"(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)


def json_default(value: Any) -> str:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def fetch_all(sql: str, params: tuple[Any, ...] = (), *, bounded_factor_join=False) -> list[dict[str, Any]]:
    with connect(admin=False) as conn:
        if bounded_factor_join:
            # Full-cohort validity remains enforced. Repeated nested-loop hash
            # checks can take minutes; this query's hash joins take under a second.
            with conn.transaction():
                conn.execute('SET TRANSACTION READ ONLY')
                conn.execute("SET LOCAL statement_timeout='25s'")
                conn.execute('SET LOCAL enable_nestloop=off')
                return [dict(row) for row in conn.execute(sql,params).fetchall()]
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def previous_market_date(report_date: dt.date) -> dt.date | None:
    rows = fetch_all(
        "SELECT max(trade_date) AS dt FROM market.trading_calendar "
        "WHERE exchange='SSE' AND session_status IN ('open','half_day') AND trade_date < %s",
        (report_date,),
    )
    return rows[0]["dt"] if rows else None


def freeze_watermarks(cutoff: dt.datetime, report_date: dt.date,
                      market_date: dt.date | None) -> tuple[list[dict[str, Any]], bool, list[str]]:
    rows = fetch_all(
        """
        SELECT dataset_key,partition_key,expected_date,data_through,row_count,
               eligible_count,coverage_pct,quality_status,schema_version,
               source_version,completed_at,details
        FROM ops.dataset_watermarks
        WHERE dataset_key=ANY(%s) AND partition_key='default' AND completed_at <= %s
        ORDER BY dataset_key,partition_key
        """,
        (list(REQUIRED_DATASETS), cutoff),
    )
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest.setdefault(row["dataset_key"], row)
    missing = [key for key in REQUIRED_DATASETS if key not in latest]
    failed = [key for key, row in latest.items() if row["quality_status"] == "fail"]
    stale: list[str] = []
    for key, row in latest.items():
        if freshness_issue(key, row, report_date, market_date):
            stale.append(key)
    blockers = [
        *(f"missing:{key}" for key in missing), *(f"failed:{key}" for key in failed),
        *(f"stale:{key}" for key in stale),
    ]
    return list(latest.values()), not blockers, blockers


def evidence_item(prefix: str, index: int, claim_type: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_id": f"{prefix}{index:04d}",
        "claim_type": claim_type,
        **value,
    }


def build_snapshot(report_date: dt.date, cutoff: dt.datetime) -> dict[str, Any]:
    market_date = previous_market_date(report_date)
    watermarks, hard_gate, blockers = freeze_watermarks(cutoff, report_date, market_date)
    import sys
    if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
    from scripts.analytics.research_snapshot import load_snapshot,compact_research
    with connect() as conn:
        shared=compact_research(load_snapshot(conn,cutoff,as_of=market_date,limit=10))
        from scripts.analytics.research_asset_registry import load_registry_summary
        experiment_registry=load_registry_summary(conn,cutoff,limit=6)
    from scripts.analytics.research_intelligence import load_intelligence,intelligence_rows
    with connect() as conn:
        intelligence=load_intelligence(conn,cutoff,as_of=report_date,limit=8,scope='focus')
    evidence: list[dict[str, Any]] = []
    for index,row in enumerate(intelligence_rows(intelligence),1):
        is_pool=row.get('component','').startswith('candidate_pool')
        evidence.append(evidence_item('CAND' if is_pool else 'FOCUS',index,'interpretation',{
            'entity_id':row.get('stock_code') or row.get('label'),'metric_id':('daily_value_plan' if row.get('component')=='candidate_pool_trade_plan' else 'candidate_pool' if row.get('component','').startswith('candidate_pool') else 'research_focus'),'value':row,
            'unit':'evidence_tracking_not_recommendation','as_of_date':row.get('as_of_date') or (intelligence.get('focus') or {}).get('as_of_date'),
            'available_at':row.get('available_at') or (intelligence.get('focus') or {}).get('decision_cutoff'),
            'source_id':'research_focus','quality_status':'unverified'}))
    for index, row in enumerate(watermarks, 1):
        evidence.append(evidence_item("DQ", index, "data_quality", {
            "entity_id": row["dataset_key"],
            "metric_id": "dataset_watermark",
            "value": {
                "data_through": row["data_through"], "row_count": row["row_count"],
                "eligible_count": row["eligible_count"], "coverage_pct": row["coverage_pct"],
                "quality_status": row["quality_status"], "details": row.get("details") or {},
            },
            "unit": "dataset", "as_of_date": row["expected_date"],
            "available_at": row["completed_at"], "source_id": row["source_version"],
            "quality_status": "verified" if row["quality_status"] == "pass" else "incomplete",
        }))

    documents = fetch_all(
        """
        SELECT d.id::text AS source_document_id,d.source_type,d.source_name,d.title,
               d.published_at,d.url,greatest(d.created_at,d.updated_at,d.published_at) AS system_available_at,
               left(coalesce(d.content_text,d.content_md,d.raw_text,''),500) AS snippet,
               q.quality_status,q.quality_score
        FROM corpus.documents d
        LEFT JOIN corpus.document_quality q ON q.document_id=d.id
        WHERE d.created_at <= %s AND d.updated_at <= %s
          AND d.published_at <= %s
          AND d.published_at >= %s::date - 7
          AND (coalesce(d.title,'') || ' ' || left(coalesce(d.content_text,d.content_md,d.raw_text,''),3000)) ~* %s
          AND coalesce(q.quality_status,'accepted') NOT IN ('template_noise','parse_failed','duplicate','quarantined')
          AND NOT EXISTS (
            SELECT 1 FROM corpus.document_quarantine x
            WHERE x.document_id=d.id AND x.released_at IS NULL
          )
        ORDER BY (d.published_at::date=%s::date) DESC,q.quality_score DESC NULLS LAST,d.published_at DESC
        LIMIT 60
        """,
        (cutoff, cutoff, cutoff, report_date, AI_HARDWARE_RE, report_date),
    )
    for index, row in enumerate(documents, 1):
        evidence.append(evidence_item("DOC", index, "document_claim", {
            "entity_id": None, "metric_id": "published_document",
            "value": {key: row[key] for key in ("title", "snippet", "source_type", "source_name", "url")},
            "unit": "document", "as_of_date": row["published_at"].date() if row["published_at"] else None,
            "available_at": row["system_available_at"], "source_id": row["source_name"],
            "source_document_id": row["source_document_id"],
            "quality_status": "verified" if row["source_type"] == "announcement" else "unverified",
        }))

    reports = fetch_all(
        """
        SELECT r.document_id::text AS source_document_id,r.report_date,r.report_title,
               r.institution,r.rating,r.rating_change,r.industry_name,r.stock_code,r.stock_name,
               greatest(r.available_at,r.created_at,r.updated_at,doc.created_at,doc.updated_at) AS available_at
        FROM research.reports_resolved r LEFT JOIN corpus.documents doc ON doc.id=r.document_id
        WHERE r.report_date BETWEEN %s::date - 14 AND %s::date
          AND greatest(r.available_at,r.created_at,r.updated_at,doc.created_at,doc.updated_at) <= %s
          AND (coalesce(r.report_title,'') || ' ' || coalesce(r.industry_name,'') || ' ' || coalesce(r.stock_name,'')) ~* %s
        ORDER BY r.report_date DESC,r.available_at DESC NULLS LAST
        LIMIT 80
        """,
        (report_date, report_date, cutoff, AI_HARDWARE_RE),
    )
    report_codes = sorted({str(row["stock_code"]) for row in reports if row.get("stock_code")} | {r["stock_code"] for r in shared.get("watchlist",[])})
    for index, row in enumerate(reports, 1):
        evidence.append(evidence_item("REP", index, "document_claim", {
            "entity_id": row["stock_code"], "metric_id": "broker_report",
            "value": {key: row[key] for key in (
                "report_title", "institution", "rating", "rating_change", "industry_name", "stock_code", "stock_name"
            )},
            "unit": "report", "as_of_date": row["report_date"], "available_at": row["available_at"],
            "source_id": row["institution"], "source_document_id": row["source_document_id"],
            "quality_status": "unverified",
        }))

    if report_codes and market_date:
        market_rows = fetch_all(
            """
            SELECT a.asset_id::text,a.symbol AS stock_code,a.name AS stock_name,b.dt,b.close,
                   b.volume,b.source,b.available_at,b.quality_status,b.price_basis
            FROM market.canonical_bars_daily_v2 b
            JOIN market.assets a ON a.asset_id=b.asset_id
            WHERE a.symbol=ANY(%s) AND b.dt=%s AND b.available_at <= %s
            ORDER BY a.symbol LIMIT 80
            """,
            (report_codes, market_date, cutoff),
        )
        for index, row in enumerate(market_rows, 1):
            evidence.append(evidence_item("MKT", index, "market", {
                "entity_id": row["stock_code"], "metric_id": "raw_close",
                "value": {"stock_code": row["stock_code"], "stock_name": row["stock_name"], "close": row["close"], "price_basis":row["price_basis"]},
                "unit": "CNY", "as_of_date": row["dt"], "available_at": row["available_at"],
                "source_id": row["source"], "quality_status": row["quality_status"],
            }))

    factors = fetch_all(
        """
        SELECT f.factor_id::text,d.factor_name,d.display_name,d.family,d.direction,
               f.dt,a.symbol AS stock_code,a.name AS stock_name,f.raw_value,f.percentile,
               f.zscore,d.lifecycle_status,f.available_at
        FROM factor.valid_daily_scores_v1 f
        JOIN factor.definitions d ON d.factor_id=f.factor_id
        JOIN market.assets a ON a.asset_id=f.asset_id
        WHERE f.available_at <= %s AND f.updated_at <= %s AND f.dt=%s AND d.lifecycle_status IN ('shadow','production')
        ORDER BY greatest(coalesce(f.percentile,0),1-coalesce(f.percentile,1)) DESC,f.dt DESC
        LIMIT 80
        """,
        (cutoff,cutoff,market_date),
        bounded_factor_join=True,
    )
    for index, row in enumerate(factors, 1):
        evidence.append(evidence_item("FAC", index, "model_signal", {
            "entity_id": row["stock_code"], "metric_id": row["factor_name"],
            "value": {key: row[key] for key in (
                "stock_code", "stock_name", "display_name", "family", "direction",
                "raw_value", "percentile", "zscore", "lifecycle_status"
            )},
            "unit": "factor_score_not_probability", "as_of_date": row["dt"],
            "available_at": row["available_at"], "source_id": "factor.valid_daily_scores_v1",
            "quality_status": "unverified",
        }))

    graph = fetch_all(
        """
        SELECT rv.relation_version_id::text,se.name source_name,se.metadata->>'stock_code' source_code,
          te.name target_name,te.metadata->>'stock_code' target_code,r.relation_type,
          lower(rv.valid_during) valid_from,upper(rv.valid_during) valid_to,
          lower(rv.public_known_during) public_known_from,rv.confidence,rv.review_status,
          CASE WHEN rv.review_status='approved' AND rv.source_system~*'(official|cninfo|sse|szse)'
               THEN 'observed_fact' WHEN coalesce(rv.extractor_version,'')~*'llm'
               THEN 'model_inference' ELSE 'extracted_claim' END claim_class,
          rv.source_system,ev.document_id::text source_document_id
        FROM kg.relation_versions rv JOIN kg.relations r USING(relation_id)
        JOIN kg.market_entities se ON se.entity_id=r.source_entity_id
        JOIN kg.market_entities te ON te.entity_id=r.target_entity_id
        JOIN LATERAL (SELECT document_id FROM kg.relation_version_evidence
          WHERE relation_version_id=rv.relation_version_id AND system_at<=%(cutoff)s AND public_at<=%(cutoff)s
          ORDER BY public_at DESC LIMIT 1) ev ON true
        WHERE rv.valid_during @> %(date)s::date AND rv.public_known_during @> %(cutoff)s::timestamptz
          AND rv.system_known_during @> %(cutoff)s::timestamptz AND rv.status='active'
          AND rv.review_status IN ('approved','classified')
          AND coalesce(rv.attributes->'attribution_check'->>'supported','true')<>'false'
          AND coalesce(rv.attributes->>'superseded_by_canonical','false')<>'true'
          AND (se.name || ' ' || te.name || ' ' || r.relation_type) ~* %(theme)s
        ORDER BY (rv.review_status='approved') DESC,rv.confidence DESC NULLS LAST
        LIMIT 60
        """,
        {'date':market_date,'cutoff':cutoff,'theme':AI_HARDWARE_RE},
    )
    for index, row in enumerate(graph, 1):
        evidence.append(evidence_item("KG", index, "graph_association", {
            "entity_id": row["source_code"] or row["source_name"], "metric_id": row["relation_type"],
            "value": {key: row[key] for key in (
                "source_name", "source_code", "target_name", "target_code", "relation_type",
                "confidence", "claim_class", "review_status", "valid_from", "valid_to"
            )},
            "unit": "graph_edge_not_causality", "as_of_date": row["valid_from"],
            "available_at": row["public_known_from"], "source_id": row["source_system"],
            "source_document_id": row["source_document_id"],
            "quality_status": "verified" if row["claim_class"] == "observed_fact" else "unverified",
        }))

    for index,item in enumerate(shared.get('watchlist',[]),1):
        evidence.append(evidence_item('POOL',index,'model_signal',{
            'entity_id':item['stock_code'],'metric_id':'frozen_research_watchlist','value':item,
            'unit':'research_watchlist_not_trade_instruction','as_of_date':shared.get('as_of_date'),
            'available_at':shared.get('decision_cutoff'),'source_id':'analytics.research_snapshot_daily',
            'quality_status':'unverified'}))
    result = {
        "shared_research":shared,
        "experiment_registry":experiment_registry,
        "research_intelligence":{
            "candidate_pool_status":(intelligence.get("candidate_pool") or {}).get("status"),
            "candidate_pool_summary":(intelligence.get("candidate_pool") or {}).get("summary"),
            "focus_status":(intelligence.get("focus") or {}).get("status"),
            "focus_cutoff":(intelligence.get("focus") or {}).get("decision_cutoff"),
            "usage":intelligence["usage"]},
        "schema_version": "ai_hardware_report_snapshot:v3",
        "report_date": report_date,
        "decision_cutoff": cutoff,
        "market_date": market_date,
        "hard_gate_passed": hard_gate,
        "hard_gate_blockers": blockers,
        "required_datasets": list(REQUIRED_DATASETS),
        "watermarks": watermarks,
        "evidence": evidence,
        "evidence_counts": {
            "documents": len(documents), "reports": len(reports), "factors": len(factors), "graph": len(graph),
        },
    }

    result["analysis_capabilities"] = analysis_capabilities(result)
    return result


def degraded_claims(snapshot: dict[str, Any], reason: str | None = None) -> list[dict[str, Any]]:
    dq_ids = [item["evidence_id"] for item in snapshot["evidence"] if item["claim_type"] == "data_quality"]
    dq_ids = dq_ids or [item['evidence_id'] for item in usable_evidence(snapshot)[:3]]
    if not dq_ids:
        return []
    if usable_evidence(snapshot):
        text = "本期保留已收录来源中的可核验信息；自动分析未能完成生成或通过核验，以下为来源摘录。"
    else:
        text = "本期尚无足够的可核验资料，等待采集与核验完成后再形成研究结论。"
    return [{"claim_id": "D001", "section_name": "数据质量与限制", "claim_type": "data_quality",
             "claim_text": text, "evidence_ids": dq_ids,
             "metadata": {"recommendation": False, "generation_mode": "source_digest"}}]


def build_prompt(snapshot_path: Path, claims_path: Path) -> str:
    allowed_sections = json.dumps(SECTIONS, ensure_ascii=False)
    return textwrap.dedent(f"""
        你是金融研究 Agent 的解释层，不是数据计算层。读取 `{snapshot_path}`，只输出结构化 claims。

        将 JSON 写入 `{claims_path}`，顶层格式严格为：
        {{"claims":[{{"claim_id":"C001","section_name":"今日核心判断","claim_type":"interpretation",
        "claim_text":"...","evidence_ids":["DOC0001"],"numeric_value":null,"unit":null,
        "as_of_date":"YYYY-MM-DD","metadata":{{"recommendation":false}}}}]}}

        规则：
        - section_name 只能从以下列表原样选择，不得自创章节名：{allowed_sections}
        - 依据 analysis_capabilities 分别判断能力；hard_gate_passed=false 仅限制买卖建议，不禁止对已收录新闻、财务事实、行情和关联作有证据的研究分析。
        - 门禁未通过时仍完成来源摘要、重要变化、相互支持或矛盾的证据、条件化解释和下一步跟踪问题；不要把整份日报写成系统故障说明。只在“数据质量与限制”简述与本期结论有关的缺口。
        - analysis_capabilities.stock_recommendations=false 时不生成“今日投资建议”章节或买卖操作指令，metadata.recommendation 必须为 false。研究关注不等同推荐买入。
        - candidate_pool 证据用于“长期候选池更新”：说明池规模和变化；有成员证据时至少选两家公司，分别解释新增、保留或淘汰原因、长期财务论点、短期趋势分和月度复核日期。没有成员变化也要解释保留依据，不能只报池规模。5/20日预测仅引用已提供的数值和到期日；排序分不是收益率。研究池可有跨行业公司，与 AI 硬件观察池分开展示。
        - candidate_pool_trade_plan 是独立的交易计划账本。核对 plan_date、mode 与 available_at：模拟观察只解释估值、盈利预测和变更依据；同业估值情景不能写成预期涨幅、买入指令或必达日期。买入上限只引用已通过验收且尚未过期的 entry_limit；未知持仓成本时不计算回本价。候选移除不代表已有持仓已卖出。当天账本在日报完成后生成，日报可引用最近已冻结记录并标明日期。
        - research_focus 证据用于“研究关注更新”和“明日跟踪点”：解释新增/修订资料、可检验的问题以及何时复核。
        - AI硬件“个股观察池”使用 shared_research.watchlist / frozen_research_watchlist 证据；“长期候选池更新”使用 candidate_pool 成员证据。分别解释记录中的入选、保留及剔除原因，不另造成员。
        - 仅 prediction_eligible=true 的情景可给出数值预测；缺失或未通过验证时省略数值预测栏目，不插入重复的“暂不提供”占位。确实影响本期结论的模型误差或输入缺口在数据说明中说一次。券商目标价单列来源和期限。
        - candidate_pool_summary.forecast_validation 提供周/月预测的真实回测误差、基线、区间覆盖率、滚动胜出次数和差值置信区间。差值区间跨零不能称为预测能力已提升，覆盖率不能称为上涨概率。用本期已核实的走势、财务和事件解释方向驱动、观察期限及验证/失效条件；不因模型未通过而省略公司分析，不把候选分数换成预期收益或目标价。
        - 模型误差和误差差值保留3位小数，覆盖率保留2位；只展示影响判断的比较结果，不抄写长小数或整份验证记录。
        - 若同时引用整体平均误差和配对误差差值，注明后者按信号日等权，不能写成前两个样本均值直接相减；也可以只报差值置信区间是否跨零。
        - 只能引用 snapshot.evidence 中存在的 evidence_id，每个事实句必须绑定证据。
        - claim_text 面向普通读者，用中文解释来源、数据日期和可靠性；不要堆砌数据库字段名、内部编号或状态码。shadow 可称“研究阶段因子”；同源价格和验证状态的共同限制集中说明一次，不逐股重复；evidence_ids 仍须保留准确编号供核验。
        - 不自行计算涨跌幅、排名、估值或统计；不得生成证据中没有的数字、日期、股票代码。
        - actual、forecast、market、model_signal、document_claim、graph_association 必须严格区分。
        - 因子 percentile 不是上涨概率；candidate/shadow/degraded 都不能写成生产信号。
        - 图谱路径只表示关联，不能写成因果；仅 observed_fact 可称已核验事实。
        - 当日证据优先于历史证据，滞后信息必须披露具体日期。
        - 个股观察必须同时引用市场证据和至少一条文档/正式披露证据。
        {READER_STYLE_PROMPT}
        - 共同数据口径与实质缺口集中在“数据质量与限制”用1-2条说明；该栏目没有实质内容时省略。不要把无关公司章程等材料凑成AI硬件催化，也不要为凑章节讨论不存在的结论。
        - 生成 12-30 条简短 claim，覆盖主要章节。不要输出 Markdown，不要修改数据库或源码。
    """).strip() + "\n"


def run_codex(run_dir: Path, prompt: str, model: str, timeout: int, reasoning_effort: str = "high") -> Path:
    codex_bin = os.getenv("CODEX_BIN", shutil.which("codex") or "codex")
    output = run_dir / "codex_last_message.txt"
    command = [
        codex_bin, "--ask-for-approval", "never", "exec", "--ignore-user-config", "--ephemeral",
        "--skip-git-repo-check", "--cd", str(run_dir), "--sandbox", "workspace-write",
        "--output-last-message", str(output), "--model", model,
        "-c", f'model_reasoning_effort="{reasoning_effort}"', "-",
    ]
    proc = subprocess.Popen(
        command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            stdout, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, _ = proc.communicate()
        (run_dir / "codex_exec.log").write_text(stdout or "", encoding="utf-8")
        raise TimeoutError(f"Codex timed out after {timeout}s")
    (run_dir / "codex_exec.log").write_text(stdout or "", encoding="utf-8")
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, command)
    return output


def run_openai_compatible(
    run_dir: Path,
    prompt: str,
    snapshot: dict[str, Any],
    model: str,
    timeout: int,
    base_url: str,
    api_key_env: str,
    reasoning_effort: str,
) -> Path:
    output = run_dir / "provider_last_message.txt"
    inline_snapshot = inline_json(compact_v3_snapshot(snapshot), max_chars=100_000)
    remote_prompt = prompt + """

当前后端不能读取本地路径。以下 inline_snapshot 是本次不可变数据快照的完整、
权威输入。必须基于它生成 claims，不得声称快照不可读或仅返回空 claims。
只引用其中真实存在的 evidence_id。

<inline_snapshot>
""" + inline_snapshot + """
</inline_snapshot>
"""
    content = openai_compatible_complete(
        [
            {
                "role": "system",
                "content": "你是金融研究解释层。只返回符合用户要求的 JSON，不输出思考过程。",
            },
            {"role": "user", "content": remote_prompt},
        ],
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
        json_object=True,
        stream=True,
        retries=2,
        audit_path=run_dir / "provider_generation.json",
    )
    output.write_text(content + "\n", encoding="utf-8")
    return output


def repair_openai_compatible_claims(
    run_dir: Path,
    snapshot: dict[str, Any],
    claims: list[dict[str, Any]],
    errors: list[str],
    model: str,
    timeout: int,
    base_url: str,
    api_key_env: str,
    reasoning_effort: str,
) -> Path:
    output = run_dir / "provider_repair_message.txt"
    repair_input = {
        "snapshot": compact_v3_snapshot(snapshot),
        "rejected_claims": claims,
        "validation_errors": errors,
        "allowed_sections": SECTIONS,
        "allowed_claim_types": sorted(CLAIM_TYPES),
    }
    prompt = READER_STYLE_PROMPT + "\n" + """
修复下面的日报 claims，只返回 {"claims":[...]} JSON。保留有证据的结论，删除或改写校验失败的内容。
section_name 和 claim_type 只能使用允许值；每条必须引用真实 evidence_id；不得新增证据中没有的数字、日期或股票代码。
不要输出思考过程、Markdown 或其他文字。

<repair_input>
""" + inline_json(repair_input, max_chars=120_000) + """
</repair_input>
"""
    content = openai_compatible_complete(
        [
            {"role": "system", "content": "你是金融证据账本的 JSON 修复层，不得放宽证据约束。"},
            {"role": "user", "content": prompt},
        ],
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        reasoning_effort=reasoning_effort,
        timeout=timeout,
        json_object=True,
        stream=True,
        retries=2,
        audit_path=run_dir / "provider_repair.json",
    )
    output.write_text(content + "\n", encoding="utf-8")
    return output


def load_claims(path: Path) -> list[dict[str, Any]]:
    preferred = path.parent / "report_claims.json"
    target = preferred if preferred.exists() else path
    text = target.read_text(encoding="utf-8").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Codex did not return a JSON object")
        text = text[start:end + 1]
    payload = json.loads(text)
    claims = payload.get("claims")
    if not isinstance(claims, list):
        raise ValueError("claims must be an array")
    return claims


def scalar_tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            tokens.update(scalar_tokens(item))
    elif isinstance(value, list):
        for item in value:
            tokens.update(scalar_tokens(item))
    elif value is not None:
        text = str(value)
        tokens.add(text)
        numeric_text = ISO_DATETIME_RE.sub("", text)
        numeric_text = ISO_DATE_RE.sub("", numeric_text)
        numeric_text = DOT_DATE_RE.sub("", numeric_text)
        numeric_text = HALF_YEAR_RANGE_RE.sub("", numeric_text)
        numeric_text = CN_DATE_RE.sub("", numeric_text)
        numeric_text = CN_YEAR_MONTH_RE.sub("", numeric_text)
        numeric_text = CN_MONTH_DAY_RE.sub("", numeric_text)
        numeric_text = PERIOD_RE.sub("",numeric_text)
        tokens.update(NUMERIC_RE.findall(numeric_text))
    return tokens


def date_tokens(value: Any) -> set[str]:
    dates: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            dates.update(date_tokens(item))
        # Different dates in one document do not authorize every combination
        # of their years and month/day components.
        period_end=str(value.get('period_end') or '')
        if re.fullmatch(r'\d{4}-06-30',period_end):dates.add(period_end[:4]+'-H1')
    elif isinstance(value, list):
        for item in value:
            dates.update(date_tokens(item))
    elif value is not None:
        text = str(value)
        for year, month, day in (
            ISO_DATE_RE.findall(text) + DOT_DATE_RE.findall(text) + CN_DATE_RE.findall(text)
        ):
            dates.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
            dates.add(f"{int(month):02d}-{int(day):02d}")
            dates.add(f"{int(year):04d}-{int(month):02d}")
        for year, month in CN_YEAR_MONTH_RE.findall(text):
            dates.add(f"{int(year):04d}-{int(month):02d}")
        for month, day in CN_MONTH_DAY_RE.findall(text):
            dates.add(f"{int(month):02d}-{int(day):02d}")
        for year,months in HALF_YEAR_RANGE_RE.findall(text):
            dates.add(year+'-H'+('1' if months.startswith('1') else '2'))
        for year,half,half_num,quarter,quarter_num in PERIOD_RE.findall(text):
            if half or half_num:dates.add(year+'-H'+(half_num or ('1' if half=='上' else '2')))
            else:
                number=quarter_num or {'一':'1','二':'2','三':'3','四':'4'}.get(quarter,quarter)
                dates.add(year+'-Q'+number)
    return dates


def normalized_numeric_token(token: str) -> str:
    raw = token.rstrip("%")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return raw
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def validate_claims(claims: list[dict[str, Any]], snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    import sys
    if str(ROOT/'MMClaw') not in sys.path: sys.path.insert(0,str(ROOT/'MMClaw'))
    from mmclaw.prediction_contract import prediction_error
    evidence = {item["evidence_id"]: item for item in snapshot["evidence"]}
    allowed_global = scalar_tokens({
        "report_date": snapshot["report_date"], "decision_cutoff": snapshot["decision_cutoff"],
        "market_date": snapshot["market_date"],
    })
    allowed_global_dates = date_tokens({
        "report_date": snapshot["report_date"], "decision_cutoff": snapshot["decision_cutoff"],
        "market_date": snapshot["market_date"],
    })
    errors: list[str] = []
    seen: set[str] = set()
    for index, claim in enumerate(claims, 1):
        claim_id = str(claim.get("claim_id") or f"C{index:03d}")
        claim["claim_id"] = claim_id
        claim_errors: list[str] = []
        if not strip_boilerplate(str(claim.get("claim_text") or "")).strip():
            claim_errors.append("empty_or_boilerplate_only_claim")
        if claim_id in seen:
            claim_errors.append("duplicate claim_id")
        seen.add(claim_id)
        section_name = str(claim.get("section_name") or "")
        if section_name in SECTION_ALIASES:
            section_name = SECTION_ALIASES[section_name]
            claim["section_name"] = section_name
        if section_name not in SECTIONS:
            claim_errors.append("invalid section_name")
        if claim.get("claim_type") not in CLAIM_TYPES:
            claim_errors.append("invalid claim_type")
        ids = claim.get("evidence_ids") or []
        if not isinstance(ids, list) or not ids:
            claim_errors.append("missing evidence_ids")
            ids = []
        missing = [item for item in ids if item not in evidence]
        if missing:
            claim_errors.append("unknown evidence_ids: " + ",".join(missing))
        selected = [evidence[item] for item in ids if item in evidence]
        allowed = set(allowed_global)
        allowed_dates = set(allowed_global_dates)
        for item in selected:
            allowed.update(scalar_tokens(evidence_numeric_projection(item)))
            allowed_dates.update(date_tokens(item))
        clean_text = re.sub(r"\[evidence:[^\]]+\]", "", str(claim.get("claim_text") or ""))
        source_error=claim_evidence_error(claim,selected)
        if source_error:claim_errors.append(source_error)
        prediction_violation=prediction_error(clean_text,selected)
        if prediction_violation: claim_errors.append(prediction_violation)
        if section_name=='个股观察池':
            pool_codes={r['stock_code'] for r in snapshot.get('shared_research',{}).get('watchlist',[])}
            pool_names={str(r.get('payload',r.get('research',{})).get('stock_name') or '') for r in snapshot.get('shared_research',{}).get('watchlist',[])}-{''}
            if (not pool_codes and claim.get('claim_type')!='data_quality') or (pool_codes and not any(code in clean_text for code in pool_codes) and not any(name in clean_text for name in pool_names)):
                claim_errors.append('claim not bound to a current frozen watchlist member')
            if set(STOCK_RE.findall(clean_text))-pool_codes:
                claim_errors.append('stock not in frozen research watchlist')
        unsupported_dates = sorted(date_tokens(clean_text) - allowed_dates)
        if unsupported_dates:
            claim_errors.append("unsupported dates: " + ",".join(unsupported_dates))
        numeric_text = ISO_DATETIME_RE.sub("", clean_text)
        numeric_text = ISO_DATE_RE.sub("", numeric_text)
        numeric_text = DOT_DATE_RE.sub("", numeric_text)
        numeric_text = HALF_YEAR_RANGE_RE.sub("", numeric_text)
        numeric_text = CN_DATE_RE.sub("", numeric_text)
        numeric_text = CN_YEAR_MONTH_RE.sub("", numeric_text)
        numeric_text = CN_MONTH_DAY_RE.sub("", numeric_text)
        numeric_text = PERIOD_RE.sub("",numeric_text)
        # A stated decline is a negative change; never accept an unsupported
        # positive increase by discarding the sign of the cited source.
        numeric_text = DECLINE_PERCENT_RE.sub(lambda m:m.group(1)+'-'+m.group(2),numeric_text)
        allowed_numbers = {
            normalized_numeric_token(token) for token in allowed if NUMERIC_RE.fullmatch(token)
        }
        unsupported_numbers = sorted({
            token for token in NUMERIC_RE.findall(numeric_text)
            if normalized_numeric_token(token) not in allowed_numbers
        })
        if unsupported_numbers:
            claim_errors.append("unsupported numbers: " + ",".join(unsupported_numbers))
        supported_codes = {token for item in selected for token in STOCK_RE.findall(json.dumps(item, default=json_default))}
        unsupported_codes = sorted(set(STOCK_RE.findall(clean_text)) - supported_codes)
        if unsupported_codes:
            claim_errors.append("unsupported stock codes: " + ",".join(unsupported_codes))
        recommendation = has_actionable_recommendation(claim)
        if recommendation and not analysis_capabilities(snapshot)["stock_recommendations"]:
            claim_errors.append("recommendation forbidden without current eligible research pool")
        claim["validation_status"] = "rejected" if claim_errors else "verified"
        claim["validation_error"] = "; ".join(claim_errors) or None
        errors.extend(f"{claim_id}: {error}" for error in claim_errors)
    return claims, errors


def render_report(report_date: dt.date, cutoff: dt.datetime, market_date: dt.date | None,
                  claims: list[dict[str, Any]], status: str, blockers: list[str]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        if claim.get("validation_status") == "verified":
            grouped[claim["section_name"]].append(claim)
    lines = [
        f"# AI硬件盘前日报 {report_date.isoformat()}", "",
        f"- 报告状态：{status}",
        f"- 决策知识截止：{cutoff.isoformat()}",
        f"- 行情基准日：{market_date.isoformat() if market_date else '无可用交易日'}", "",
    ]
    for section in SECTIONS:
        section_claims = grouped.get(section, [])
        if not section_claims:
            continue
        lines.extend([f"## {section}", ""])
        for claim in section_claims:
            refs = ", ".join(claim["evidence_ids"])
            lines.append(f"- {claim['claim_text']} [evidence: {refs}]")
        lines.append("")
    if blockers:
        lines.extend(["### 硬门禁阻断项", "", *[f"- `{item}`" for item in blockers], ""])
    return "\n".join(lines).rstrip() + "\n"


def persist_claims(report_run_id: uuid.UUID, claims: list[dict[str, Any]]) -> None:
    with connect(admin=False) as conn:
        for claim in claims:
            conn.execute(
                """
                INSERT INTO agent.report_claims (
                  report_run_id,claim_id,section_name,claim_type,claim_text,evidence_ids,
                  numeric_value,unit,as_of_date,validation_status,validation_error,metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (report_run_id,claim_id) DO UPDATE SET
                  section_name=EXCLUDED.section_name,claim_type=EXCLUDED.claim_type,
                  claim_text=EXCLUDED.claim_text,evidence_ids=EXCLUDED.evidence_ids,
                  numeric_value=EXCLUDED.numeric_value,unit=EXCLUDED.unit,
                  as_of_date=EXCLUDED.as_of_date,validation_status=EXCLUDED.validation_status,
                  validation_error=EXCLUDED.validation_error,metadata=EXCLUDED.metadata
                """,
                (
                    report_run_id, claim["claim_id"], claim.get("section_name") or "数据质量与限制",
                    claim.get("claim_type") if claim.get("claim_type") in CLAIM_TYPES else "interpretation",
                    str(claim.get("claim_text") or ""), claim.get("evidence_ids") or [],
                    claim.get("numeric_value"), claim.get("unit"), claim.get("as_of_date"),
                    claim.get("validation_status") or "pending", claim.get("validation_error"),
                    Jsonb(claim.get("metadata") or {}),
                ),
            )


def generate_report_claims(snapshot, snapshot_json, run_dir, args):
    """Generate evidence analysis even when recommendations cannot be issued."""
    errors = []
    repair_attempted = False
    generation_mode = "model_evidence_analysis"
    claims = []
    if args.skip_model or not usable_evidence(snapshot):
        generation_mode = "source_digest"
        errors.append("model skipped" if args.skip_model else "no substantive evidence")
        claims = source_digest(snapshot) + degraded_claims(snapshot)
    else:
        try:
            claims_target = run_dir / "report_claims.json"
            prompt = build_prompt(run_dir / "data_snapshot.json", claims_target)
            (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            if args.provider == "codex":
                output = run_codex(run_dir, prompt, args.model, args.timeout, args.reasoning_effort)
            else:
                output = run_openai_compatible(run_dir, prompt, snapshot_json, args.model,
                    args.timeout, args.base_url, args.api_key_env, args.reasoning_effort)
            claims = load_claims(output)
            minimum = min(8, max(1, len(usable_evidence(snapshot))))
            if len(claims) < minimum:
                raise ValueError(f"model returned too few claims: {len(claims)}; expected at least {minimum}")
            claims, initial_errors = validate_claims(claims, snapshot)
            errors.extend(initial_errors)
            if initial_errors and args.provider != "codex":
                repair_attempted = True
                repair_output = repair_openai_compatible_claims(run_dir, snapshot_json, claims,
                    initial_errors, args.model, args.timeout, args.base_url, args.api_key_env, args.reasoning_effort)
                repaired = load_claims(repair_output)
                if len(repaired) < minimum:
                    raise ValueError(f"repair returned too few claims: {len(repaired)}; expected at least {minimum}")
                repaired, repair_errors = validate_claims(repaired, snapshot)
                original_good={c['claim_id']:c for c in claims if c.get('validation_status')=='verified'}
                merged={c['claim_id']:c for c in repaired}
                for key,good in original_good.items():
                    if key not in merged or merged[key].get('validation_status')!='verified':
                        merged[key]=good
                claims=list(merged.values())
                errors.extend(repair_errors)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            # Retain already verified useful claims if only the repair/provider
            # failed; raw failure detail remains in the audit file.
            if not any(c.get('validation_status') == 'verified' and c.get('claim_type') != 'data_quality' for c in claims):
                claims = source_digest(snapshot) + degraded_claims(snapshot)
                generation_mode = "source_digest"
    claims, final_errors = validate_claims(claims, snapshot)
    errors.extend(e for e in final_errors if e not in errors)
    verified = [c for c in claims if c.get('validation_status') == 'verified']
    substantive = [c for c in verified if c.get('claim_type') != 'data_quality']
    if not verified:
        status = 'quarantined'
    elif generation_mode == 'source_digest' or final_errors or not snapshot['hard_gate_passed']:
        status = 'degraded'
    elif substantive:
        status = 'published'
    else:
        status = 'degraded'
    return claims, status, errors, repair_attempted, generation_mode


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate evidence-bound AI hardware daily report V3.")
    parser.add_argument("--date")
    parser.add_argument("--provider", default=os.getenv("FINANCE_DAILY_PROVIDER", "openai_compatible"))
    parser.add_argument("--model", default=os.getenv("FINANCE_DAILY_MODEL", os.getenv("CODEX_DAILY_MODEL", "gpt-6-astra")))
    parser.add_argument("--base-url", default=os.getenv(
        "FINANCE_DAILY_BASE_URL", os.getenv("FINANCE_API_BASE_URL", "http://127.0.0.1:8082/v1")
    ))
    parser.add_argument("--api-key-env", default=os.getenv("FINANCE_DAILY_API_KEY_ENV", "FINANCE_API_KEY"))
    parser.add_argument("--reasoning-effort", default=os.getenv("FINANCE_DAILY_REASONING_EFFORT", "high"))
    parser.add_argument("--timeout", type=int, default=int(os.getenv("FINANCE_DAILY_TIMEOUT_SECONDS", os.getenv("CODEX_DAILY_TIMEOUT_SECONDS", "720"))))
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--publish", action="store_true", help="Publish to the official daily directory after validation.")
    args = parser.parse_args()
    report_date = dt.date.fromisoformat(args.date) if args.date else today()
    cutoff = dt.datetime.now(TZ) if report_date == today() else dt.datetime.combine(report_date, dt.time(8, 50), TZ)
    run_id = uuid.uuid4()
    report_type = "ai_hardware_daily_v3" if args.publish else "ai_hardware_daily_v3_shadow"
    run_dir = RUNS_DIR / report_date.isoformat() / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=False)

    snapshot = build_snapshot(report_date, cutoff)
    snapshot_json = json.loads(json.dumps(snapshot, ensure_ascii=False, default=json_default))
    snapshot_path = run_dir / "data_snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2, default=json_default) + "\n", encoding="utf-8")
    snapshot_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    with connect(admin=False) as conn:
        conn.execute(
            """
            INSERT INTO agent.report_runs (
              report_run_id,report_date,report_type,status,decision_cutoff,market_date,
              data_snapshot,model_provider,model_name,validation
            ) VALUES (%s,%s,%s,'building',%s,%s,%s,%s,%s,%s)
            """,
            (run_id, report_date, report_type, cutoff, snapshot["market_date"], Jsonb(snapshot_json), args.provider, args.model,
             Jsonb({"snapshot_sha256": snapshot_hash, "hard_gate_passed": snapshot["hard_gate_passed"]})),
        )

    claims, status, errors, repair_attempted, generation_mode = generate_report_claims(
        snapshot, snapshot_json, run_dir, args)

    persist_claims(run_id, claims)
    report_text = render_report(
        report_date, cutoff, snapshot["market_date"], claims, status, snapshot["hard_gate_blockers"],
    )
    run_report = run_dir / "report.md"
    run_report.write_text(report_text, encoding="utf-8")
    output_dir = DAILY_DIR if args.publish and status in {"published", "degraded"} else SHADOW_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{report_date.isoformat()}.md"
    publish_readable_report(run_report, output_path, snapshot_path)
    validation = {
        "snapshot_sha256": snapshot_hash, "hard_gate_passed": snapshot["hard_gate_passed"],
        "blockers": snapshot["hard_gate_blockers"], "claim_count": len(claims),
        "verified_claims": sum(c.get("validation_status") == "verified" for c in claims),
        "errors": errors, "repair_attempted": repair_attempted, "shadow": not args.publish,
        "generation_mode": generation_mode, "analysis_capabilities": snapshot.get("analysis_capabilities"),
        "unresolved_errors": [f"{c.get('claim_id')}: {c.get('validation_error')}" for c in claims if c.get('validation_status')=='rejected'],
        "status": status,
        "model": args.model, "reasoning_effort": args.reasoning_effort, "provider": args.provider,
    }
    (run_dir / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, default=json_default) + "\n", encoding="utf-8",
    )
    with connect(admin=False) as conn:
        conn.execute(
            """
            UPDATE agent.report_runs SET status=%s,validation=%s,output_path=%s,completed_at=now()
            WHERE report_run_id=%s
            """,
            (status, Jsonb(validation), str(output_path), run_id),
        )
    print(json.dumps({"report_run_id": str(run_id), "status": status, "output_path": str(output_path), **validation}, ensure_ascii=False))
    return 0 if status in {"published", "degraded"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
