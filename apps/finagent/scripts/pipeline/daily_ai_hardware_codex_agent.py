#!/usr/bin/env python3
from __future__ import annotations

import sys

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import datetime as dt
import json
import os
import re
import signal
import shutil
import subprocess
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from common import ROOT, TZ, connect, iso, load_env, log_path, run_logged, today
from openai_compatible_client import complete as openai_compatible_complete
from daily_llm_payload import compact_legacy_data_pack, inline_json
from user_presentation import publish_readable_report, READER_STYLE_PROMPT


ANALYSIS_DIR = ROOT / "analysis/AI_Hardware"
DAILY_DIR = ANALYSIS_DIR / "daily"
RUNS_DIR = ANALYSIS_DIR / "runs"
AGENT_MD = ANALYSIS_DIR / "Agent.md"
DEFAULT_CODEX_MODEL = "gpt-6-astra"
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_CODEX_TIMEOUT_SECONDS = 720
REPORT_SECTIONS = (
    "省流版",
    "今日核心判断",
    "今日新增新闻与催化",
    "主题热度变化",
    "细分产业链观察",
    "个股观察池",
    "关键证据",
    "今日投资建议",
    "明日跟踪点",
    "数据质量与限制",
)
MUTATING_SQL_RE = re.compile(
    r"\b(insert|update|delete|alter|drop|truncate|create|grant|revoke|copy|vacuum|call|do|merge|refresh|reindex|cluster)\b",
    re.I,
)

AI_TERMS_RE = r"(AI|人工智能|算力|大模型|英伟达|NVIDIA|智能计算|智算)"
HARDWARE_TERMS_RE = (
    r"(GPU|ASIC|CPO|光模块|光芯片|光通信|光纤|PCB|HDI|封装|先进封装|HBM|存储|DDR|"
    r"服务器|数据中心|液冷|电源|功率半导体|半导体|芯片|晶圆|光刻|刻蚀|薄膜|靶材|"
    r"半导体材料|电子材料|光刻胶|六氟化钨|MPO|MLCC|玻璃基板|TGV|铜缆|高速连接|交换机|硅光)"
)


def json_default(value: Any) -> str:
    return str(value)


def fetch_all(sql: str, params: tuple[Any, ...] = (), *, bounded_factor_join: bool = False) -> list[dict[str, Any]]:
    with connect(admin=False) as conn:
        if bounded_factor_join:
            # Correlated validity predicates can estimate one row and repeat
            # the full daily cohort hash for every score in a nested loop.
            # This transaction-local plan choice keeps the same validity view.
            conn.execute("SET LOCAL enable_nestloop=off")
            conn.execute("SET LOCAL statement_timeout='60s'")
        return conn.execute(sql, params).fetchall()


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    rows = fetch_all(sql, params)
    return rows[0] if rows else {}


def decision_cutoff(report_date: dt.date) -> dt.datetime:
    """Return the knowledge cutoff for a pre-market report.

    Live runs use their actual start time. Historical replays are frozen at
    09:00 China time, the publication gate immediately before the 09:05 push,
    so a later database state cannot silently redefine the information set.
    """
    now = dt.datetime.now(TZ)
    if report_date == now.date():
        return now
    return dt.datetime.combine(report_date, dt.time(9, 0), tzinfo=TZ)


def previous_market_date(report_date: dt.date) -> dt.date | None:
    row = fetch_one(
        """
        SELECT max(dt) AS dt
        FROM market.canonical_bars_daily
        WHERE dt < %s::date
        """,
        (report_date,),
    )
    return row.get("dt")


def dynamic_ai_hardware_pattern() -> str:
    rows = fetch_all(
        """
        SELECT name, aliases
        FROM kg.concepts
        WHERE status = 'active'
          AND ai_hardware_relevance >= 0.45
        ORDER BY ai_hardware_relevance DESC, name
        LIMIT 240
        """
    )
    terms: set[str] = set()
    for row in rows:
        for term in [row.get("name"), *(row.get("aliases") or [])]:
            value = str(term or "").strip()
            if 2 <= len(value) <= 32:
                terms.add(value)
    # The bootstrap vocabulary keeps the report operational while the graph is
    # being backfilled; active graph concepts extend it automatically.
    bootstrap = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", HARDWARE_TERMS_RE)
    terms.update(x for x in bootstrap if len(x) >= 2)
    return "(" + "|".join(re.escape(x) for x in sorted(terms, key=lambda x: (-len(x), x))) + ")"


def prior_report_summary(path: Path, max_chars: int = 1600) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    selected: list[str] = []
    keep = False
    for line in text.splitlines():
        if line.startswith("## "):
            keep = line[3:].strip() in {"省流版", "今日核心判断", "明日跟踪点", "数据质量与限制"}
        if keep:
            selected.append(line)
    summary = "\n".join(selected).strip()
    return {
        "path": str(path),
        "name": path.name,
        "mtime": dt.datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat(),
        "summary": summary[:max_chars],
    }


def build_data_pack(report_date: dt.date, cutoff_at: dt.datetime | None = None) -> dict[str, Any]:
    cutoff_at = cutoff_at or decision_cutoff(report_date)
    market_date = previous_market_date(report_date)
    ai_hardware_re = dynamic_ai_hardware_pattern()
    recent_start = report_date - dt.timedelta(days=7)
    report_start = report_date - dt.timedelta(days=14)

    latest_dates = fetch_one(
        """
        SELECT
          (
            SELECT greatest(
              (
                SELECT max(a.newspaper_date)
                FROM news.articles a
                JOIN corpus.documents d ON d.id = a.document_id
                WHERE a.newspaper_date <= %s AND d.created_at <= %s
              ),
              (
                SELECT max((d.published_at AT TIME ZONE 'Asia/Shanghai')::date)
                FROM corpus.documents d
                WHERE d.source_type IN ('news','peoples_daily')
                  AND d.published_at < ((%s::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
                  AND d.created_at <= %s
                  AND NOT EXISTS (SELECT 1 FROM news.articles a WHERE a.document_id = d.id)
              )
            )
          ) AS latest_news_date,
          (SELECT max(report_date) FROM research.reports WHERE report_date <= %s AND coalesce(available_at, created_at) <= %s) AS latest_report_date,
          (SELECT max(as_of_date) FROM analytics.ai_hardware_theme_scores WHERE as_of_date <= %s AND updated_at <= %s) AS latest_theme_date,
          (SELECT max(dt) FROM analytics.asset_technical_daily WHERE dt <= %s AND coalesce(available_at, updated_at) <= %s) AS latest_technical_date,
          (SELECT max(dt) FROM analytics.asset_signal_daily WHERE dt <= %s AND updated_at <= %s) AS latest_signal_date,
          (SELECT max(event_date) FROM analytics.document_events WHERE event_date <= %s AND created_at <= %s) AS latest_event_date
        """
        ,
        (
            report_date, cutoff_at, report_date, cutoff_at,
            report_date, cutoff_at,
            report_date, cutoff_at,
            report_date, cutoff_at,
            report_date, cutoff_at,
            report_date, cutoff_at,
        ),
    )
    latest_news_date = latest_dates.get("latest_news_date") or report_date
    source_freshness = fetch_all(
        """
        WITH docs AS (
          SELECT d.source_type, d.source_name, coalesce(a.newspaper_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) AS doc_date
          FROM corpus.documents d
          LEFT JOIN news.articles a ON a.document_id = d.id
          WHERE d.source_type IN ('news','peoples_daily')
            AND d.created_at <= %s
        )
        SELECT source_type, source_name,
               max(doc_date) AS latest_date,
               count(*) FILTER (WHERE doc_date = %s) AS rows_today,
               count(*) FILTER (WHERE doc_date BETWEEN %s AND %s) AS rows_7d
        FROM docs
        GROUP BY source_type, source_name
        ORDER BY latest_date DESC NULLS LAST, rows_today DESC, rows_7d DESC, source_name
        """,
        (cutoff_at, report_date, recent_start, report_date),
    )
    source_counts = fetch_all(
        """
        WITH docs AS (
          SELECT d.source_type, d.source_name, coalesce(a.newspaper_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) AS doc_date
          FROM corpus.documents d
          LEFT JOIN news.articles a ON a.document_id = d.id
          WHERE d.source_type IN ('news','peoples_daily')
            AND d.created_at <= %s
        )
        SELECT source_type, source_name, count(*) AS rows
        FROM docs
        WHERE doc_date BETWEEN %s AND %s
        GROUP BY source_type, source_name
        ORDER BY rows DESC, source_type, source_name
        """,
        (cutoff_at, recent_start, report_date),
    )
    same_day_news = fetch_all(
        """
        WITH docs AS (
          SELECT d.id, a.newspaper_date AS doc_date,
                 d.source_type, d.source_name, d.title, d.url,
                 coalesce(a.page_name, a.section_name, a.topic, d.metadata->>'channel') AS section,
                 left(coalesce(d.content_text, d.content_md, d.raw_text, ''), 2000) AS body
          FROM news.articles a
          JOIN corpus.documents d ON d.id = a.document_id
          WHERE a.newspaper_date = %s
            AND d.created_at <= %s
          UNION ALL
          SELECT d.id, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date AS doc_date,
                 d.source_type, d.source_name, d.title, d.url,
                 d.metadata->>'channel' AS section,
                 left(coalesce(d.content_text, d.content_md, d.raw_text, ''), 2000) AS body
          FROM corpus.documents d
          WHERE d.source_type IN ('news','peoples_daily')
            AND d.published_at >= (%s::date::timestamp AT TIME ZONE 'Asia/Shanghai')
            AND d.published_at < ((%s::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
            AND d.created_at <= %s
            AND NOT EXISTS (SELECT 1 FROM news.articles a WHERE a.document_id = d.id)
        ),
        scored AS (
          SELECT id, doc_date, source_type, source_name, title, url, section,
                 left(body, 320) AS snippet,
                 (
                   CASE WHEN coalesce(title, '') ~* %s THEN 3 ELSE 0 END
                   + CASE WHEN coalesce(title, '') ~* %s THEN 5 ELSE 0 END
                   + CASE WHEN body ~* %s THEN 1 ELSE 0 END
                   + CASE WHEN body ~* %s THEN 2 ELSE 0 END
                   + CASE WHEN (
                       (coalesce(title, '') ~* %s AND (coalesce(title, '') || ' ' || body) ~* %s)
                       OR (coalesce(title, '') ~* %s AND (coalesce(title, '') || ' ' || body) ~* %s)
                     ) THEN 8 ELSE 0 END
                 ) AS relevance_score,
                 CASE
                   WHEN (
                       (coalesce(title, '') ~* %s AND (coalesce(title, '') || ' ' || body) ~* %s)
                       OR (coalesce(title, '') ~* %s AND (coalesce(title, '') || ' ' || body) ~* %s)
                     ) THEN 'direct_ai_hardware'
                   WHEN (coalesce(title, '') || ' ' || body) ~* %s THEN 'hardware_related'
                   WHEN (coalesce(title, '') || ' ' || body) ~* %s THEN 'broad_ai'
                   ELSE 'other'
                 END AS relevance_bucket
          FROM docs
        )
        SELECT s.id AS document_id, s.doc_date, s.source_type, s.source_name, s.title, s.section, s.url,
               s.relevance_score, s.relevance_bucket,
               coalesce(
                 (SELECT array_agg(k.keyword ORDER BY k.weight DESC)
                  FROM (
                    SELECT keyword, max(weight) AS weight
                    FROM corpus.document_keywords
                    WHERE document_id = s.id
                    GROUP BY keyword
                    ORDER BY max(weight) DESC
                    LIMIT 8
                  ) k),
                 ARRAY[]::text[]
               ) AS keywords,
               s.snippet
        FROM scored s
        WHERE s.relevance_score > 0
        ORDER BY s.relevance_score DESC, s.source_name, s.title
        LIMIT 30
        """,
        (
            report_date,
            cutoff_at,
            report_date,
            report_date,
            cutoff_at,
            AI_TERMS_RE,
            ai_hardware_re,
            AI_TERMS_RE,
            ai_hardware_re,
            ai_hardware_re,
            AI_TERMS_RE,
            AI_TERMS_RE,
            ai_hardware_re,
            ai_hardware_re,
            AI_TERMS_RE,
            AI_TERMS_RE,
            ai_hardware_re,
            ai_hardware_re,
            AI_TERMS_RE,
        ),
    )
    latest_available_news = fetch_all(
        """
        WITH docs AS (
          SELECT d.id, a.newspaper_date AS doc_date,
                 d.source_type, d.source_name, d.title, d.url,
                 coalesce(a.page_name, a.section_name, a.topic, d.metadata->>'channel') AS section,
                 left(coalesce(d.content_text, d.content_md, d.raw_text, ''), 2000) AS body
          FROM news.articles a
          JOIN corpus.documents d ON d.id = a.document_id
          WHERE a.newspaper_date = %s
            AND d.created_at <= %s
          UNION ALL
          SELECT d.id, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date AS doc_date,
                 d.source_type, d.source_name, d.title, d.url,
                 d.metadata->>'channel' AS section,
                 left(coalesce(d.content_text, d.content_md, d.raw_text, ''), 2000) AS body
          FROM corpus.documents d
          WHERE d.source_type IN ('news','peoples_daily')
            AND d.published_at >= (%s::date::timestamp AT TIME ZONE 'Asia/Shanghai')
            AND d.published_at < ((%s::date + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
            AND d.created_at <= %s
            AND NOT EXISTS (SELECT 1 FROM news.articles a WHERE a.document_id = d.id)
        ),
        scored AS (
          SELECT id, doc_date, source_type, source_name, title, url, section,
                 left(body, 280) AS snippet,
                 (
                   CASE WHEN coalesce(title, '') ~* %s THEN 3 ELSE 0 END
                   + CASE WHEN coalesce(title, '') ~* %s THEN 5 ELSE 0 END
                   + CASE WHEN body ~* %s THEN 1 ELSE 0 END
                   + CASE WHEN body ~* %s THEN 2 ELSE 0 END
                   + CASE WHEN (
                       (coalesce(title, '') ~* %s AND (coalesce(title, '') || ' ' || body) ~* %s)
                       OR (coalesce(title, '') ~* %s AND (coalesce(title, '') || ' ' || body) ~* %s)
                     ) THEN 8 ELSE 0 END
                 ) AS relevance_score,
                 CASE
                   WHEN (
                       (coalesce(title, '') ~* %s AND (coalesce(title, '') || ' ' || body) ~* %s)
                       OR (coalesce(title, '') ~* %s AND (coalesce(title, '') || ' ' || body) ~* %s)
                     ) THEN 'direct_ai_hardware'
                   WHEN (coalesce(title, '') || ' ' || body) ~* %s THEN 'hardware_related'
                   WHEN (coalesce(title, '') || ' ' || body) ~* %s THEN 'broad_ai'
                   ELSE 'other'
                 END AS relevance_bucket
          FROM docs
        )
        SELECT id AS document_id, doc_date, source_type, source_name, title, section, url, relevance_score, relevance_bucket, snippet
        FROM scored
        WHERE relevance_score > 0
        ORDER BY relevance_score DESC, source_name, title
        LIMIT 20
        """,
        (
            latest_news_date,
            cutoff_at,
            latest_news_date,
            latest_news_date,
            cutoff_at,
            AI_TERMS_RE,
            ai_hardware_re,
            AI_TERMS_RE,
            ai_hardware_re,
            ai_hardware_re,
            AI_TERMS_RE,
            AI_TERMS_RE,
            ai_hardware_re,
            ai_hardware_re,
            AI_TERMS_RE,
            AI_TERMS_RE,
            ai_hardware_re,
            ai_hardware_re,
            AI_TERMS_RE,
        ),
    )
    same_day_keywords = fetch_all(
        """
        WITH docs AS (
          SELECT d.id
          FROM corpus.documents d
          LEFT JOIN news.articles a ON a.document_id = d.id
          WHERE d.source_type IN ('news','peoples_daily')
            AND coalesce(a.newspaper_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) = %s
            AND d.created_at <= %s
        )
        SELECT k.keyword,
               count(*) AS doc_count,
               round(avg(k.weight)::numeric, 3) AS avg_weight,
               count(*) FILTER (WHERE k.keyword ~* %s) AS ai_hardware_hits
        FROM docs
        JOIN corpus.document_keywords k ON k.document_id = docs.id
        WHERE char_length(k.keyword) >= 2
          AND k.keyword NOT IN ('公司','行业','市场','中国','发展','相关','证券','投资','同比','实现','表示','业务')
        GROUP BY k.keyword
        ORDER BY ai_hardware_hits DESC, doc_count DESC, avg_weight DESC, k.keyword
        LIMIT 30
        """,
        (report_date, cutoff_at, ai_hardware_re),
    )
    event_counts = fetch_all(
        """
        SELECT event_type, polarity, count(*) AS rows, round(avg(impact_score)::numeric, 3) AS avg_impact
        FROM analytics.document_events
        WHERE event_date BETWEEN %s AND %s
          AND created_at <= %s
        GROUP BY event_type, polarity
        ORDER BY rows DESC, avg_impact DESC
        LIMIT 30
        """,
        (recent_start, report_date, cutoff_at),
    )
    same_day_events = fetch_all(
        """
        SELECT id AS event_id, document_id, event_date, event_type, polarity, round(impact_score::numeric, 2) AS impact,
               confidence, stock_codes, industries, keywords, summary, left(evidence, 240) AS evidence
        FROM analytics.document_events
        WHERE event_date = %s AND created_at <= %s
        ORDER BY impact_score DESC NULLS LAST, confidence DESC NULLS LAST
        LIMIT 30
        """,
        (report_date, cutoff_at),
    )
    latest_available_events = fetch_all(
        """
        WITH latest AS (
          SELECT max(event_date) AS event_date
          FROM analytics.document_events
          WHERE event_date <= %s AND created_at <= %s
        )
        SELECT e.id AS event_id, e.document_id, e.event_date, e.event_type, e.polarity, round(e.impact_score::numeric, 2) AS impact,
               e.confidence, e.stock_codes, e.industries, e.keywords, e.summary, left(e.evidence, 300) AS evidence
        FROM analytics.document_events e
        JOIN latest l ON l.event_date = e.event_date
        ORDER BY e.impact_score DESC NULLS LAST, e.confidence DESC NULLS LAST
        LIMIT 20
        """,
        (report_date, cutoff_at),
    )
    top_events = fetch_all(
        """
        SELECT id AS event_id, document_id, event_date, event_type, polarity, round(impact_score::numeric, 2) AS impact,
               confidence, stock_codes, industries, keywords, summary, left(evidence, 240) AS evidence
        FROM analytics.document_events
        WHERE event_date BETWEEN %s AND %s
          AND created_at <= %s
          AND event_type IN (
            'policy_support','government_investment','order_contract','price_hike',
            'demand_growth','earnings_positive','external_risk','regulation_risk','price_cut'
          )
        ORDER BY impact_score DESC NULLS LAST, confidence DESC NULLS LAST, event_date DESC
        LIMIT 40
        """,
        (recent_start, report_date, cutoff_at),
    )
    theme_summary = fetch_all(
        """
        SELECT c.name AS theme, count(DISTINCT s.stock_code) AS stock_count,
               sum(s.mention_count_30d)::int AS mentions_30d,
               sum(s.evidence_count_90d)::int AS evidence_90d,
               round(avg(s.forward_pe)::numeric, 2) AS avg_forward_pe,
               round(avg(s.forecast_eps_growth)::numeric, 4) AS avg_forecast_eps_growth,
               round(max(s.score)::numeric, 2) AS max_score,
               (array_agg(s.stock_name ORDER BY s.score DESC))[1:8] AS top_stocks
        FROM analytics.ai_hardware_theme_scores s
        JOIN kg.concepts c ON c.concept_id = s.concept_id
        WHERE s.as_of_date = (
          SELECT max(as_of_date) FROM analytics.ai_hardware_theme_scores
          WHERE as_of_date <= %s AND updated_at <= %s
        )
        GROUP BY c.name
        HAVING count(DISTINCT s.stock_code) >= 2
        ORDER BY mentions_30d DESC, max_score DESC
        LIMIT 30
        """,
        (report_date, cutoff_at),
    )
    top_theme_stocks = fetch_all(
        """
        WITH ranked AS (
          SELECT s.*, c.name AS theme,
                 row_number() OVER (PARTITION BY s.stock_code ORDER BY s.score DESC) AS rn
          FROM analytics.ai_hardware_theme_scores s
          JOIN kg.concepts c ON c.concept_id = s.concept_id
          WHERE s.as_of_date = (
            SELECT max(as_of_date) FROM analytics.ai_hardware_theme_scores
            WHERE as_of_date <= %s AND updated_at <= %s
          )
        )
        SELECT stock_code, stock_name, theme, relation_type, round(score::numeric, 2) AS score,
               forward_pe, round(forecast_eps_growth::numeric, 4) AS forecast_eps_growth,
               latest_rating, latest_rating_change, mention_count_30d, evidence_count_90d,
               research_report_count_180d, left((evidence->'evidence'->>0), 360) AS evidence
        FROM ranked
        WHERE rn = 1
        ORDER BY score DESC NULLS LAST
        LIMIT 40
        """,
        (report_date, cutoff_at),
    )
    recent_reports = fetch_all(
        """
        SELECT document_id, report_date, report_type, stock_code, stock_name, industry_name, institution,
               rating, rating_change, report_title
        FROM research.reports
        WHERE report_date BETWEEN %s AND %s
          AND coalesce(available_at, created_at) <= %s
          AND (
            report_title ~* %s
            OR industry_name ~* '(半导体|通信|计算机|电子|专用设备|光学光电子|元件|非金属材料|小金属)'
          )
        ORDER BY report_date DESC, stock_code NULLS LAST, institution
        LIMIT 40
        """,
        (report_start, report_date, cutoff_at, ai_hardware_re),
    )
    reports_today = fetch_all(
        """
        SELECT document_id, report_date, report_type, stock_code, stock_name, industry_name, institution,
               rating, rating_change, report_title
        FROM research.reports
        WHERE report_date = %s
          AND coalesce(available_at, created_at) <= %s
          AND (
            report_title ~* %s
            OR industry_name ~* '(半导体|通信|计算机|电子|专用设备|光学光电子|元件|非金属材料|小金属)'
          )
        ORDER BY stock_code NULLS LAST, institution, report_title
        LIMIT 40
        """,
        (report_date, cutoff_at, ai_hardware_re),
    )
    fusion_top = fetch_all(
        """
        SELECT stock_code, stock_name, round(composite_score::numeric, 2) AS composite_score,
               signal_tags, evidence
        FROM analytics.asset_signal_daily
        WHERE dt = (
          SELECT max(dt) FROM analytics.asset_signal_daily
          WHERE dt <= %s AND updated_at <= %s
        )
          AND composite_score > 0
        ORDER BY composite_score DESC
        LIMIT 30
        """,
        (report_date, cutoff_at),
    )
    factor_catalog = fetch_all(
        """
        WITH latest_eval AS (
          SELECT DISTINCT ON (e.factor_id)
                 e.factor_id, e.sample_name, e.observation_count, e.date_count,
                 round(e.coverage::numeric, 4) AS coverage,
                 round(e.rank_ic_mean::numeric, 5) AS rank_ic_mean,
                 round(e.rank_ic_ir::numeric, 4) AS rank_ic_ir,
                 round(e.q_value::numeric, 5) AS q_value,
                 e.passed_gates, r.test_start, r.test_end, r.horizon_days
          FROM factor.evaluations e
          JOIN factor.experiment_runs r ON r.run_id = e.run_id
          WHERE r.status = 'complete' AND r.test_end < %s
            AND e.sample_name = 'test'
            AND r.finished_at <= %s
            AND r.config->>'validation_state' IS DISTINCT FROM 'superseded'
          ORDER BY e.factor_id, r.test_end DESC, r.started_at DESC, e.created_at DESC
        )
        SELECT d.factor_name, d.display_name, d.family, d.description,
               d.lifecycle_status, d.direction,
               le.sample_name, le.observation_count, le.date_count, le.coverage,
               le.rank_ic_mean, le.rank_ic_ir, le.q_value, le.passed_gates,
               le.test_start, le.test_end, le.horizon_days
        FROM factor.definitions d
        LEFT JOIN latest_eval le ON le.factor_id = d.factor_id
        WHERE d.lifecycle_status IN ('shadow', 'production')
        ORDER BY CASE d.lifecycle_status WHEN 'production' THEN 0 ELSE 1 END,
                 le.passed_gates DESC NULLS LAST, abs(le.rank_ic_mean) DESC NULLS LAST,
                 d.factor_name
        """,
        (report_date, cutoff_at),
    )
    candidate_codes = [str(row["stock_code"]) for row in top_theme_stocks if row.get("stock_code")]
    factor_extremes: list[dict[str, Any]] = []
    factor_score_date = None
    if market_date and candidate_codes:
        # Resolve a concrete day first; joining a max-date CTE to the valid
        # score view can aggregate the full historical cohort-version table.
        factor_score_date = fetch_one(
            """SELECT max(h.dt) AS dt FROM factor.factor_health_daily h
               JOIN factor.definitions d USING(factor_id)
               WHERE h.dt BETWEEN %s::date-14 AND %s::date
                 AND h.latest_available_at<=%s AND h.computed_at<=%s
                 AND d.lifecycle_status IN ('shadow','production')""",
            (market_date, market_date, cutoff_at, cutoff_at),
        ).get("dt")
        factor_extremes = fetch_all(
            """
            SELECT a.symbol AS stock_code, a.name AS stock_name,
                   d.factor_name, d.display_name, d.family, d.lifecycle_status,
                   s.dt AS score_date, s.source_feature_date, s.available_at,
                   round(s.percentile::numeric, 4) AS percentile,
                   round(s.zscore::numeric, 4) AS zscore
            FROM factor.valid_daily_scores_v1 s
            JOIN factor.definitions d ON d.factor_id = s.factor_id
            JOIN market.assets a ON a.asset_id = s.asset_id
            WHERE s.dt = %s::date AND a.symbol = ANY(%s)
              AND s.available_at <= %s
              AND s.source_decision_cutoff <= %s
              AND d.lifecycle_status IN ('shadow', 'production')
              AND (s.percentile >= 0.85 OR s.percentile <= 0.15)
            ORDER BY abs(s.percentile - 0.5) DESC, a.symbol, d.factor_name
            LIMIT 60
            """,
            (factor_score_date, candidate_codes, cutoff_at, cutoff_at),
            bounded_factor_join=True,
        )
    temporal_graph_evidence: list[dict[str, Any]] = []
    if candidate_codes:
        temporal_graph_evidence = fetch_all(
            """
            WITH entity_ids AS (
              SELECT array_agg(entity_id) AS ids
              FROM kg.market_entities
              WHERE metadata->>'stock_code' = ANY(%s)
            ), edges AS (
              SELECT e.*
              FROM entity_ids i
              CROSS JOIN LATERAL kg.temporal_edges_at(
                %s, %s, 'public', i.ids, NULL, 0.55, 120
              ) e
            )
            SELECT edges.relation_version_id, edges.source_name, edges.source_type,
                   edges.relation_type, edges.target_name, edges.target_type,
                   edges.direction, round(edges.confidence::numeric, 4) AS confidence,
                   edges.valid_during, edges.public_known_during,
                   ev.evidence_id, ev.document_id, ev.public_at AS evidence_public_at,
                   left(ev.evidence_text, 260) AS evidence_text
            FROM edges
            LEFT JOIN LATERAL (
              SELECT e.evidence_id, e.document_id, e.public_at, e.evidence_text
              FROM kg.relation_version_evidence e
              WHERE e.relation_version_id = edges.relation_version_id
                AND (e.public_at IS NULL OR e.public_at <= %s)
              ORDER BY e.confidence DESC NULLS LAST, e.public_at DESC NULLS LAST
              LIMIT 1
            ) ev ON true
            ORDER BY edges.confidence DESC, edges.source_name, edges.relation_type
            LIMIT 60
            """,
            (candidate_codes, report_date, cutoff_at, cutoff_at),
        )
    graph_coverage = fetch_all(
        """
        SELECT source_name, dt, expected_documents, rules_processed, llm_processed,
               relation_count, status, extractor_version, updated_at
        FROM kg.source_coverage_daily
        WHERE dt BETWEEN %s AND %s
        ORDER BY dt DESC, source_name
        LIMIT 80
        """,
        (recent_start, report_date),
    )
    import sys
    if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
    from scripts.analytics.research_snapshot import load_snapshot,compact_research
    with connect() as conn:
        shared_research=compact_research(load_snapshot(conn,cutoff_at,as_of=market_date,limit=10))
        from scripts.analytics.research_asset_registry import load_registry_summary
        experiment_registry=load_registry_summary(conn,cutoff_at,limit=6)
    prior_reports = []
    if DAILY_DIR.exists():
        for path in sorted(DAILY_DIR.glob("*.md"), reverse=True):
            if path.name == f"{iso(report_date)}.md":
                continue
            prior_reports.append(prior_report_summary(path))
            if len(prior_reports) >= 4:
                break

    return {
        "shared_research":shared_research,
        "experiment_registry":experiment_registry,
        "report_date": iso(report_date),
        "generated_at": dt.datetime.now(TZ).isoformat(),
        "decision_cutoff": cutoff_at.isoformat(),
        "forecast_field_definitions": {
            "forecast_eps_growth": "同一研报相邻预测年度的每股收益增速；EPS 单位元/股。",
            "avg_forecast_eps_growth": "公司预测 EPS 增速的均值。",
            "forecast_net_profit": "原研报明确给出的归母净利润总额，单位人民币元。",
            "forward_pe": "原研报明确年度的 PE，使用研报引用价格；不是最新股价计算的 PE。",
        },
        "expected_market_date": iso(market_date) if market_date else None,
        "time_semantics": {
            "news": "Only documents ingested by decision_cutoff are included.",
            "reports": "report_date <= report_date and available_at <= decision_cutoff.",
            "market": "The expected pre-market price/technical date is expected_market_date, not report_date.",
            "factors": "Scores require dt <= expected_market_date and available_at <= decision_cutoff.",
            "graph": "Edges must be valid on report_date and publicly knowable by decision_cutoff.",
        },
        "latest_dates": latest_dates,
        "source_freshness": source_freshness,
        "source_counts_7d": source_counts,
        "same_day_news": same_day_news,
        "latest_available_news_if_today_empty": [] if same_day_news else latest_available_news,
        "same_day_news_keywords": same_day_keywords,
        "event_counts_7d": event_counts,
        "same_day_events": same_day_events,
        "latest_available_events_if_today_empty": [] if same_day_events else latest_available_events,
        "top_events_7d": top_events,
        "theme_summary": theme_summary,
        "top_theme_stocks": top_theme_stocks,
        "same_day_ai_hardware_reports": reports_today,
        "recent_ai_hardware_reports": recent_reports,
        "fusion_top": fusion_top,
        "factor_catalog": factor_catalog,
        "factor_score_date": iso(factor_score_date) if factor_score_date else None,
        "factor_extremes": factor_extremes,
        "temporal_graph_evidence": temporal_graph_evidence,
        "temporal_graph_coverage": graph_coverage,
        "prior_daily_reports": prior_reports,
        "notes": [
            "Use shared_research.watchlist for the AI-hardware watchlist. Add a separate long-term candidate-pool update from candidate_pool evidence: membership changes, long-term/timing scores and monthly thesis reviews. Use stored entry/removal reasons and only supplied forecast numbers; do not invent memberships or returns.",
            "Only scenario.prediction_eligible=true permits a numeric model forecast. Missing/blocked fields stay unavailable. Broker targets remain separately attributed; never substitute them for model expected returns.",
            "Use same_day_news, same_day_events, same_day_news_keywords, and same_day_ai_hardware_reports before 7-day aggregates.",
            "If same-day sections are empty, use latest_available_* as a stale fallback and explicitly state the fallback date.",
            "Weight direct same-day AI hardware news/events about 3x over 7-day aggregate evidence; use stale data only as background.",
            "Use query_db.py for additional read-only SQL analysis when the data pack lacks a required detail.",
            "The final report should cite concrete dates and metrics and state stale data limitations.",
            "candidate factors are excluded; shadow factors are observational and percentiles are not probabilities.",
            "Temporal graph paths are associations, not proof of causality; use evidence_id and coverage status.",
        ],
    }


def write_query_helper(run_dir: Path) -> Path:
    helper = run_dir / "query_db.py"
    helper.write_text(
        textwrap.dedent(
            f"""\
            #!{str(ROOT / sys.executable)}
            from __future__ import annotations

            import argparse
            import json
            import re
            import sys
            import time
            import uuid
            from pathlib import Path

            RUN_DIR = Path({str(run_dir)!r})
            REQUEST_DIR = RUN_DIR / "query_requests"
            MUTATING_RE = re.compile(r"\\b(insert|update|delete|alter|drop|truncate|create|grant|revoke|copy|vacuum|call|do|merge|refresh|reindex|cluster)\\b", re.I)

            def validate(sql: str) -> None:
                cleaned = sql.strip().rstrip(";").strip()
                if not cleaned:
                    raise SystemExit("empty SQL")
                if ";" in cleaned:
                    raise SystemExit("only one SQL statement is allowed")
                if MUTATING_RE.search(cleaned):
                    raise SystemExit("mutating SQL is not allowed")
                if not re.match(r"^(select|with)\\b", cleaned, re.I):
                    raise SystemExit("only SELECT/WITH queries are allowed")

            def main() -> int:
                parser = argparse.ArgumentParser()
                parser.add_argument("sql", nargs="?", help="SQL string. If omitted, stdin is used.")
                parser.add_argument("--limit", type=int, default=200)
                args = parser.parse_args()
                sql = args.sql or sys.stdin.read()
                validate(sql)
                REQUEST_DIR.mkdir(parents=True, exist_ok=True)
                request_id = uuid.uuid4().hex
                request_path = REQUEST_DIR / f"{{request_id}}.request.json"
                response_path = REQUEST_DIR / f"{{request_id}}.response.json"
                temp_path = request_path.with_suffix(".tmp")
                temp_path.write_text(
                    json.dumps({{"sql": sql, "limit": max(1, min(args.limit, 500))}}, ensure_ascii=False),
                    encoding="utf-8",
                )
                temp_path.replace(request_path)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if response_path.exists():
                        response = json.loads(response_path.read_text(encoding="utf-8"))
                        response_path.unlink(missing_ok=True)
                        if not response.get("ok"):
                            raise SystemExit(response.get("error") or "query broker failed")
                        print(json.dumps(response.get("rows", []), ensure_ascii=False, default=str, indent=2))
                        return 0
                    time.sleep(0.1)
                raise SystemExit("query broker timeout after 45s")
                return 0

            if __name__ == "__main__":
                raise SystemExit(main())
            """
        ),
        encoding="utf-8",
    )
    helper.chmod(0o640)
    wrapper = run_dir / "query_db"
    wrapper.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            cd {str(ROOT)!r}
            exec {sys.executable!r} {str(helper)!r} "$@"
            """
        ),
        encoding="utf-8",
    )
    wrapper.chmod(0o750)
    return helper


def _validate_readonly_sql(sql: str) -> str:
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned or ";" in cleaned:
        raise ValueError("exactly one SQL statement is required")
    if MUTATING_SQL_RE.search(cleaned) or not re.match(r"^(select|with)\b", cleaned, re.I):
        raise ValueError("only read-only SELECT/WITH queries are allowed")
    return cleaned


def _readonly_broker_connect():
    env = load_env()
    return psycopg.connect(
        host=os.getenv("FINANCE_PGHOST", "127.0.0.1"),
        port=int(os.getenv("FINANCE_PGPORT", "15432")),
        dbname=os.getenv("FINANCE_PGDATABASE", env.get("POSTGRES_DB", "finance_corpus")),
        user=os.getenv("AI_READONLY_DB_USER", env.get("AI_READONLY_DB_USER", "finance_ai_reader")),
        password=os.getenv("AI_READONLY_DB_PASSWORD", env.get("AI_READONLY_DB_PASSWORD", "")),
        options="-c default_transaction_read_only=on -c statement_timeout=30000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=30000",
        row_factory=dict_row,
        connect_timeout=10,
    )


def serve_query_requests(run_dir: Path, stop_event: threading.Event) -> None:
    request_dir = run_dir / "query_requests"
    request_dir.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set():
        requests = sorted(request_dir.glob("*.request.json"))
        if not requests:
            stop_event.wait(0.1)
            continue
        for request_path in requests:
            response_path = request_path.with_name(request_path.name.replace(".request.json", ".response.json"))
            response: dict[str, Any]
            try:
                payload = json.loads(request_path.read_text(encoding="utf-8"))
                sql = _validate_readonly_sql(str(payload.get("sql") or ""))
                limit = max(1, min(int(payload.get("limit") or 200), 500))
                wrapped = f"SELECT * FROM ({sql}) q LIMIT {limit}"
                with _readonly_broker_connect() as conn:
                    conn.execute("SET TRANSACTION READ ONLY")
                    rows = conn.execute(wrapped).fetchall()
                encoded = json.dumps(rows, ensure_ascii=False, default=json_default)
                if len(encoded.encode("utf-8")) > 2_000_000:
                    raise ValueError("query output exceeds 2 MB; add filters or select fewer columns")
                response = {"ok": True, "rows": rows}
            except Exception as exc:  # noqa: BLE001 - returned to the sandboxed helper
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            temp_path = response_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(response, ensure_ascii=False, default=json_default),
                encoding="utf-8",
            )
            temp_path.replace(response_path)
            request_path.unlink(missing_ok=True)


def build_prompt(
    report_date: dt.date,
    run_dir: Path,
    draft_path: Path,
    daily_path: Path,
    cutoff_at: dt.datetime,
    market_date: dt.date | None,
) -> str:
    agent_text = AGENT_MD.read_text(encoding="utf-8")
    return textwrap.dedent(
        f"""\
        你是 AI 硬件每日研究 Agent。请严格按下面流程生成盘前日报。

        <agent_instructions>
        {agent_text}
        </agent_instructions>

        今日报告日期：{iso(report_date)}
        决策知识截止时间：{cutoff_at.isoformat()}
        盘前行情预期日期：{iso(market_date) if market_date else '无可用交易日'}

        当前 Codex 工作目录：{ROOT}
        每日运行目录：{run_dir}
        已生成数据包：{run_dir / "data_pack.json"}
        只读数据库查询助手：{run_dir / "query_db"}
        报告草稿路径：{draft_path}
        验证通过后由 runner 发布到：{daily_path}

        任务：
        1. 阅读 `{run_dir / "data_pack.json"}`。
        2. 先分析 time_semantics、decision_cutoff、expected_market_date、source_freshness 和 same_day_*；这些字段决定“今日新增”和时效性。
        3. 如果当天字段为空，只能把 latest_available_* 作为滞后补位，并在报告里写清楚补位日期，不能当成今日新增。
        4. 如需补充分析，使用 `{run_dir / "query_db"} "<SQL>"` 执行只读 SQL。你可以在每日运行目录创建临时 .sql/.py/.json/.csv/.md 文件。
        5. 补充 SQL 是可选项；如果 query_db 失败，继续使用 data_pack.json，并在“数据质量与限制”里说明。
        6. 使用 factor_catalog、factor_extremes 时必须披露 lifecycle_status、score_date 和验证区间；shadow 只能作为观察证据，percentile 不是上涨概率。
        7. 使用 temporal_graph_evidence 时引用 relation_version_id/evidence_id，并结合 temporal_graph_coverage；图谱关联不能表述成因果。
        8. 对比 prior_daily_reports 中已压缩的 summary，判断今日新增、延续和弱化，不要重新读取整篇历史日报。
        9. 写出草稿 `{draft_path}`，结构必须符合 Agent.md。不要直接修改正式日报。
        10. 最后回复简短说明草稿已生成，并列出 3-5 条核心结论。

        约束：
        - 只能写每日运行目录；正式 daily 目录由 runner 发布。
        - 禁止修改仓库源码、schema、配置、爬虫、systemd、数据库。
        - SQL 必须只读。
        - 所有事实必须满足 decision_cutoff；不得使用 report_date 之后或截止时间之后才公开的数据。
        - 盘前行情停留在 expected_market_date 是正常状态，不能误报为数据滞后。
        - 每个进入观察池的股票必须同时写股票代码、行情日期、证据日期和至少一个可核验依据。
        - 明确区分“数据库事实”“模型推断”和“风险假设”。证据不足时放弃方向判断。
        - 如果数据滞后或缺失，必须在报告里明确说明。
        - 正文面向普通读者：用中文解释数据日期、来源、可靠性和限制，不堆砌数据库字段名、工具名或内部状态码。shadow 可称“研究阶段因子”；同源价格和验证状态的共同限制集中说明一次，不逐股重复。
        - 草稿引用仍保留完整且准确的文档/证据编号供核对；发布时会转换为简短的来源标注。不要输出哈希值或请求追踪编号。
        - 权重顺序：报告日直接 AI 硬件新闻/事件最高；上一日或最近可用新闻只能作为补位；7 日统计用于确认趋势；30/90 日证据只做背景。
        {READER_STYLE_PROMPT}
        - 共同资料限制只集中说明一次，不为填满章节重复“本期不提供”。无可发布内容的章节省略。
        - 不要编造数据库里没有的事实。
        """
    )


def refresh_analysis(report_date: dt.date, log_file: Path) -> None:
    since_3 = iso(report_date - dt.timedelta(days=3))
    since_14 = iso(report_date - dt.timedelta(days=14))
    since_30 = iso(report_date - dt.timedelta(days=30))
    until = iso(report_date)
    commands = [
        [sys.executable, "scripts/analytics/extract_document_events_rules.py", "--since", since_3, "--until", until, "--limit", "10000"],
        [sys.executable, "scripts/analytics/build_research_signals.py", "--since", since_14, "--until", until],
        [sys.executable, "scripts/analytics/build_technical_signals.py", "--since", since_14, "--until", until],
        [sys.executable, "scripts/analytics/seed_ai_hardware_graph.py"],
        [sys.executable, "scripts/analytics/discover_ai_hardware_concepts.py", "--since", since_30, "--until", until, "--limit", "20000"],
        [sys.executable, "scripts/analytics/classify_ai_hardware_candidates.py", "--rules-only", "--limit", "10000"],
        [sys.executable, "scripts/analytics/promote_ai_hardware_candidates.py", "--min-confidence", "0.75", "--limit", "10000"],
        [sys.executable, "scripts/analytics/extract_research_target_price.py", "--since", since_30, "--limit", "5000"],
        [sys.executable, "scripts/analytics/build_ai_hardware_theme_scores.py", "--as-of", until],
        [sys.executable, "scripts/analytics/build_fusion_signals.py", "--since", since_14, "--until", until],
    ]
    for command in commands:
        run_logged(command, log_file)


def run_codex(
    run_dir: Path,
    prompt_path: Path,
    output_path: Path,
    codex_bin: str,
    model: str,
    reasoning_effort: str,
    timeout_seconds: int,
) -> None:
    command = [
        codex_bin,
        "--ask-for-approval",
        "never",
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--cd",
        str(run_dir),
        "--sandbox",
        "workspace-write",
        "--output-last-message",
        str(output_path),
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
    ]
    command.append("-")
    prompt = prompt_path.read_text(encoding="utf-8")
    broker_stop = threading.Event()
    broker_thread = threading.Thread(
        target=serve_query_requests,
        args=(run_dir, broker_stop),
        name="daily-report-query-broker",
        daemon=True,
    )
    broker_thread.start()
    try:
        with (run_dir / "codex_exec.log").open("a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                output, _ = proc.communicate(prompt, timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    output, _ = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    output, _ = proc.communicate()
                log.write(output or "")
                raise TimeoutError(f"Codex daily report timed out after {timeout_seconds}s")
            print(output, end="")
            log.write(output)
            code = proc.returncode
    finally:
        broker_stop.set()
        broker_thread.join(timeout=5)
    if code:
        raise subprocess.CalledProcessError(code, command)


def run_openai_compatible_daily(
    prompt_path: Path,
    data_pack_path: Path,
    output_path: Path,
    draft_path: Path,
    model: str,
    reasoning_effort: str,
    timeout_seconds: int,
    base_url: str,
    api_key_env: str,
) -> None:
    data_pack = json.loads(data_pack_path.read_text(encoding="utf-8"))
    inline_pack = inline_json(compact_legacy_data_pack(data_pack))
    prompt = prompt_path.read_text(encoding="utf-8") + """

当前后端不能操作本地文件或数据库。下面的 inline_data_pack 是本次运行从
data_pack.json 提取的权威输入。必须实际使用其中数据；不得声称数据包不可读、
未提供或需要访问本地路径。不要复述任务，不要输出思考过程；请直接返回
report_draft.md 的完整 Markdown 正文，并严格只使用 inline_data_pack 中已有证据。

<inline_data_pack>
""" + inline_pack + """
</inline_data_pack>
"""
    content = openai_compatible_complete(
        [
            {"role": "system", "content": "你是证据约束的金融日报生成器，只输出最终 Markdown。"},
            {"role": "user", "content": prompt},
        ],
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        reasoning_effort=reasoning_effort,
        timeout=timeout_seconds,
        stream=True,
        retries=2,
        audit_path=draft_path.parent / "provider_generation.json",
    )
    if content.startswith("```"):
        content = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", content, flags=re.S)
    output_path.write_text(content + "\n", encoding="utf-8")
    draft_path.write_text(content + "\n", encoding="utf-8")


def validate_report(path: Path, report_date: dt.date, market_date: dt.date | None) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Codex did not create report draft: {path}")
    text = path.read_text(encoding="utf-8", errors="strict")
    warnings: list[str] = []
    fatal_errors: list[str] = []
    if not (2500 <= len(text) <= 40000):
        warnings.append(f"report length outside 2500..40000 characters: {len(text)}")
    expected_title = f"# AI硬件盘前日报 {iso(report_date)}"
    if not text.lstrip().startswith(expected_title):
        warnings.append(f"report title should start with: {expected_title}")
    missing_sections = [name for name in REPORT_SECTIONS if f"## {name}" not in text]
    if missing_sections:
        warnings.append("report missing sections: " + ", ".join(missing_sections))
    cutoff_markers = (
        "数据时点",
        "decision_cutoff",
        "决策知识截止",
        "决策截止",
        "截止时间",
        "知识截止",
    )
    if not any(marker in text for marker in cutoff_markers):
        warnings.append("report does not disclose its data cutoff")
    if market_date and iso(market_date) not in text:
        warnings.append(f"report does not disclose expected market date {market_date}")
    # Exclude six-digit fragments embedded in UUIDs/hashes while accepting
    # ordinary Markdown forms such as 公司（300308）.
    stock_codes = sorted(
        set(re.findall(r"(?<![0-9A-Fa-f-])(?:[0368]\d{5})(?![0-9A-Fa-f-])", text))
    )
    unknown_codes: list[str] = []
    if stock_codes:
        try:
            rows = fetch_all(
                "SELECT symbol FROM market.assets WHERE asset_type = 'stock' AND symbol = ANY(%s)",
                (stock_codes,),
            )
            known = {str(row["symbol"]) for row in rows}
            unknown_codes = sorted(set(stock_codes) - known)
        except Exception as exc:
            warnings.append(f"stock-code validation unavailable: {type(exc).__name__}: {exc}")
    if unknown_codes:
        warnings.append("report contains unknown stock codes: " + ", ".join(unknown_codes))
    inaccessible_markers = (
        "无法读取数据包", "数据包不可读取", "当前对话未提供 `data_pack.json`",
        "当前对话未提供data_pack.json", "未能读取 `data_pack.json`",
    )
    if any(marker in text for marker in inaccessible_markers):
        fatal_errors.append("remote model claimed the inline data pack was unavailable")
    return {
        "characters": len(text),
        "sections": len(REPORT_SECTIONS) - len(missing_sections),
        "stock_codes": stock_codes,
        "unknown_stock_codes": unknown_codes,
        "market_date": iso(market_date) if market_date else None,
        "validation_status": (
            "failed" if fatal_errors else ("passed" if not warnings else "published_with_warnings")
        ),
        "warnings": warnings,
        "fatal_errors": fatal_errors,
    }


def deterministic_degraded_report(
    report_date: dt.date,
    market_date: dt.date | None,
    cutoff_at: dt.datetime,
    reason: str,
) -> str:
    lines = [
        f"# AI硬件盘前日报 {report_date.isoformat()}", "",
        "- 报告状态：degraded",
        f"- 决策知识截止：{cutoff_at.isoformat()}",
        f"- 行情基准日：{market_date.isoformat() if market_date else '无可用交易日'}", "",
    ]
    # One operational notice, not ten empty analytical/recommendation sections.
    # The raw reason is retained in the audited draft and redacted on delivery.
    lines.extend(["## 数据更新说明", "", f"- 模型生成降级原因：{reason}", ""])
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Codex-powered daily AI hardware research agent.")
    parser.add_argument("--date", help="Report date YYYY-MM-DD. Defaults to today in Asia/Shanghai.")
    parser.add_argument("--skip-refresh", action="store_true", help="Do not refresh analytics tables before building the data pack.")
    parser.add_argument("--skip-codex", action="store_true", help="Build run files but do not invoke Codex.")
    parser.add_argument(
        "--publish-existing-draft",
        action="store_true",
        help="Validate and publish an existing report_draft.md without rerunning refresh or Codex.",
    )
    parser.add_argument("--codex-bin", default=os.getenv("CODEX_BIN", shutil.which("codex") or "codex"))
    parser.add_argument("--provider", default=os.getenv("FINANCE_DAILY_PROVIDER", "openai_compatible"))
    parser.add_argument("--model", default=os.getenv("FINANCE_DAILY_MODEL", os.getenv("CODEX_DAILY_MODEL", DEFAULT_CODEX_MODEL)))
    parser.add_argument("--base-url", default=os.getenv(
        "FINANCE_DAILY_BASE_URL", os.getenv("FINANCE_API_BASE_URL", "http://127.0.0.1:8082/v1")
    ))
    parser.add_argument("--api-key-env", default=os.getenv("FINANCE_DAILY_API_KEY_ENV", "FINANCE_API_KEY"))
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high"),
        default=os.getenv("FINANCE_DAILY_REASONING_EFFORT", os.getenv("CODEX_DAILY_REASONING_EFFORT", DEFAULT_REASONING_EFFORT)),
    )
    parser.add_argument(
        "--codex-timeout",
        type=int,
        default=int(os.getenv("FINANCE_DAILY_TIMEOUT_SECONDS", os.getenv("CODEX_DAILY_TIMEOUT_SECONDS", str(DEFAULT_CODEX_TIMEOUT_SECONDS)))),
    )
    args = parser.parse_args()

    report_date = dt.date.fromisoformat(args.date) if args.date else today()
    market_date = previous_market_date(report_date)
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = RUNS_DIR / iso(report_date)
    run_dir.mkdir(parents=True, exist_ok=True)
    daily_path = DAILY_DIR / f"{iso(report_date)}.md"
    draft_path = run_dir / "report_draft.md"
    validation_path = run_dir / "report_validation.json"
    output_path = run_dir / "codex_output.md"
    prompt_path = run_dir / "prompt.md"
    data_pack_path = run_dir / "data_pack.json"

    if args.publish_existing_draft:
        recovery_cutoff = None
        if data_pack_path.exists():
            try:
                saved_pack = json.loads(data_pack_path.read_text(encoding="utf-8"))
                saved_cutoff = saved_pack.get("decision_cutoff")
                if saved_cutoff:
                    recovery_cutoff = str(saved_cutoff)
            except Exception:
                recovery_cutoff = None
        validation = validate_report(draft_path, report_date, market_date)
        validation.update(
            {
                "validated_at": dt.datetime.now(TZ).isoformat(),
                "decision_cutoff": recovery_cutoff or decision_cutoff(report_date).isoformat(),
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "recovery_mode": True,
            }
        )
        validation_path.write_text(
            json.dumps(validation, ensure_ascii=False, indent=2, default=json_default) + "\n",
            encoding="utf-8",
        )
        publish_readable_report(draft_path, daily_path, data_pack_path)
        print(f"[ai_hardware_agent] recovered and published {daily_path}")
        return 0

    log_file = log_path("ai_hardware_codex_agent")
    if not args.skip_refresh:
        refresh_analysis(report_date, log_file)

    cutoff_at = decision_cutoff(report_date)
    data_pack = build_data_pack(report_date, cutoff_at=cutoff_at)
    data_pack_path.write_text(
        json.dumps(data_pack, ensure_ascii=False, separators=(",", ":"), default=json_default) + "\n",
        encoding="utf-8",
    )
    write_query_helper(run_dir)
    prompt_path.write_text(
        build_prompt(report_date, run_dir, draft_path, daily_path, cutoff_at, market_date),
        encoding="utf-8",
    )

    if args.skip_codex:
        print(f"[ai_hardware_agent] prepared run dir: {run_dir}")
        print(f"[ai_hardware_agent] data pack: {data_pack_path}")
        print(f"[ai_hardware_agent] prompt: {prompt_path}")
        return 0

    draft_path.unlink(missing_ok=True)
    if args.provider == "codex":
        run_codex(
            run_dir, prompt_path, output_path, args.codex_bin, args.model,
            args.reasoning_effort, max(60, args.codex_timeout),
        )
    else:
        try:
            run_openai_compatible_daily(
                prompt_path, data_pack_path, output_path, draft_path,
                args.model, args.reasoning_effort, max(60, args.codex_timeout),
                args.base_url, args.api_key_env,
            )
        except Exception as exc:  # noqa: BLE001
            draft_path.write_text(
                deterministic_degraded_report(
                    report_date, market_date, cutoff_at,
                    f"{type(exc).__name__}: {str(exc)[:500]}",
                ),
                encoding="utf-8",
            )
    validation = validate_report(draft_path, report_date, market_date)
    if validation["fatal_errors"]:
        reason = "; ".join(validation["fatal_errors"])
        draft_path.write_text(
            deterministic_degraded_report(report_date, market_date, cutoff_at, reason),
            encoding="utf-8",
        )
        validation = validate_report(draft_path, report_date, market_date)
        validation["validation_status"] = "degraded"
        validation["generation_error"] = reason
    validation.update(
        {
            "validated_at": dt.datetime.now(TZ).isoformat(),
            "decision_cutoff": cutoff_at.isoformat(),
            "model": args.model,
            "reasoning_effort": args.reasoning_effort,
        }
    )
    validation_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, default=json_default) + "\n",
        encoding="utf-8",
    )
    publish_readable_report(draft_path, daily_path, data_pack_path)
    print(f"[ai_hardware_agent] validated and published {daily_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
