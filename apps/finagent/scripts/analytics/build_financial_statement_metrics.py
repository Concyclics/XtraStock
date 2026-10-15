# -*- coding: utf-8 -*-
"""Extract actual quarterly financial metrics from research report text.

This is intentionally conservative: only explicit same-sentence metric/yoy
pairs are materialized. Forecast rows are not used here.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import date

from common import connect


METRICS = {
    "revenue": ("营业收入", "营收", "收入"),
    "net_profit_parent": ("归母净利润", "归母净利", "归属于母公司股东的净利润"),
    "deducted_net_profit": ("扣非归母净利润", "扣非净利润", "扣非净利"),
}

SENTENCE_SPLIT_RE = re.compile(r"[。；;\n]+")
VALUE_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*(亿元|亿|万元|万)?")
YOY_RE = re.compile(
    r"(?:同比|同环比|同/环比|同环比分别|同比增长|同比下降|同比下滑|同比减少)"
    r"[^-+0-9]{0,8}([+-]?\d+(?:\.\d+)?)\s*[%％]"
)
FORECAST_WORDS = (
    "盈利预测",
    "我们预计",
    "预计公司",
    "预计202",
    "预测",
    "维持",
    "目标价",
    "对应PE",
    "对应pe",
    "投资评级",
)
SEGMENT_MARKERS = (
    "其中",
    "分产品",
    "分渠道",
    "分业务",
    "分地区",
    "分板块",
    "分品牌",
    "分品类",
    "子公司",
    "平台",
)
COMPANY_TOTAL_MARKERS = ("公司", "集团", "本公司", "上市公司")


@dataclass
class ExtractedMetric:
    metric_name: str
    metric_value: float | None
    metric_yoy: float
    source_text: str
    confidence: float
    scope: str


def report_period_from_arg(value: str) -> tuple[int, int, str]:
    match = re.fullmatch(r"(\d{4})Q([1-4])", value.strip(), re.I)
    if not match:
        raise ValueError("report period must look like 2026Q1")
    year = int(match.group(1))
    quarter = int(match.group(2))
    return year, quarter, f"{year}Q{quarter}"


def normalize_value(raw: str, unit: str | None) -> float:
    value = float(raw)
    if unit in {"万元", "万"}:
        return value / 10000.0
    return value


def parse_yoy(sentence: str, metric_pos: int) -> float | None:
    tail = sentence[metric_pos : metric_pos + 140]
    for match in YOY_RE.finditer(tail):
        value = float(match.group(1))
        phrase = match.group(0)
        prefix = tail[max(0, match.start() - 8) : match.start()]
        if any(word in (prefix + phrase) for word in ("下降", "下滑", "减少")):
            value = -abs(value)
        return value / 100.0
    return None


def parse_value(sentence: str, metric_pos: int) -> float | None:
    tail = sentence[metric_pos : metric_pos + 80]
    for match in VALUE_RE.finditer(tail):
        start = match.start()
        context = tail[max(0, start - 6) : start]
        if "%" in tail[match.start() : match.end() + 1] or "％" in tail[match.start() : match.end() + 1]:
            continue
        if any(word in context for word in ("同比", "环比", "增长", "下降", "下滑")):
            continue
        return normalize_value(match.group(1), match.group(2))
    return None


def metric_scope(sentence: str) -> str:
    if any(marker in sentence for marker in SEGMENT_MARKERS):
        return "segment"
    if any(marker in sentence for marker in COMPANY_TOTAL_MARKERS):
        return "company_total"
    return "unknown"


def extract_metrics(text: str) -> list[ExtractedMetric]:
    results: list[ExtractedMetric] = []
    seen = set()
    compact_text = re.sub(r"\s+", "", text or "")
    for sentence in SENTENCE_SPLIT_RE.split(compact_text):
        if len(sentence) < 8:
            continue
        if any(word in sentence for word in FORECAST_WORDS):
            continue
        if "分别为" in sentence and sentence.count("/") >= 2:
            continue
        scope = metric_scope(sentence)
        if scope != "company_total":
            continue
        for metric_name, aliases in METRICS.items():
            for alias in aliases:
                pos = sentence.find(alias)
                if pos < 0:
                    continue
                yoy = parse_yoy(sentence, pos)
                if yoy is None:
                    continue
                value = parse_value(sentence, pos)
                source_text = sentence[:320]
                key = (metric_name, round(yoy, 6), source_text)
                if key in seen:
                    continue
                seen.add(key)
                confidence = 0.95 if value is not None else 0.85
                results.append(ExtractedMetric(metric_name, value, yoy, source_text, confidence, scope))
                break
    return results


def preferred_sources(price_adjustment: str) -> list[tuple[str, str, bool]]:
    if price_adjustment == "none":
        return [("eastmoney:kline:day:none", "none", False), ("eastmoney:kline:day:qfq", "qfq", True)]
    return [("eastmoney:kline:day:qfq", "qfq", False), ("eastmoney:kline:day:none", "none", True)]


def upsert_event_returns(conn, period: str, latest_date: str | None, price_adjustment: str) -> int:
    price_source = "analytics.asset_technical_daily"
    adjustment = "technical"
    docs = conn.execute(
        """
        SELECT DISTINCT ON (m.stock_code, m.source_document_id)
               m.asset_id, m.stock_code, m.stock_name, m.report_date, m.source_document_id
        FROM company.financial_statement_metrics m
        WHERE m.report_period = %(period)s
          AND m.asset_id IS NOT NULL
          AND m.report_date IS NOT NULL
        ORDER BY m.stock_code, m.source_document_id, m.confidence DESC NULLS LAST
        """,
        {"period": period},
    ).fetchall()
    if not docs:
        return 0

    asset_ids = sorted({row["asset_id"] for row in docs})
    print(f"event_returns: docs={len(docs)}, assets={len(asset_ids)}, loading latest prices", flush=True)
    latest_by_key = {}
    for index, asset_id in enumerate(asset_ids, start=1):
        if index % 100 == 0:
            print(f"event_returns: latest prices {index}/{len(asset_ids)}", flush=True)
        latest = conn.execute(
            """
            SELECT dt, close
            FROM analytics.asset_technical_daily
            WHERE asset_id = %(asset_id)s
              AND (%(latest_date)s::date IS NULL OR dt <= %(latest_date)s::date)
            ORDER BY dt DESC
            LIMIT 1
            """,
            {"asset_id": asset_id, "latest_date": latest_date},
        ).fetchone()
        if latest:
            latest_by_key[asset_id] = latest

    upsert_rows = []
    print("event_returns: loading base prices", flush=True)
    for index, row in enumerate(docs, start=1):
        if index % 100 == 0:
            print(f"event_returns: base prices {index}/{len(docs)}", flush=True)
        base = conn.execute(
            """
            SELECT dt, close
            FROM analytics.asset_technical_daily
            WHERE asset_id = %(asset_id)s
              AND dt >= %(event_date)s::date
              AND dt <= %(event_date)s::date + 14
            ORDER BY dt ASC
            LIMIT 1
            """,
            {"asset_id": row["asset_id"], "event_date": row["report_date"]},
        ).fetchone()
        latest = latest_by_key.get(row["asset_id"])
        base_close = float(base["close"]) if base else None
        latest_close = float(latest["close"]) if latest else None
        return_pct = (latest_close / base_close - 1.0) if base_close and latest_close else None
        upsert_rows.append(
            {
                "asset_id": row["asset_id"],
                "stock_code": row["stock_code"],
                "stock_name": row["stock_name"],
                "event_date": row["report_date"],
                "base_trade_date": base["dt"] if base else None,
                "latest_trade_date": latest["dt"] if latest else None,
                "base_close": base_close,
                "latest_close": latest_close,
                "return_pct": return_pct,
                "price_source": price_source,
                "adjustment": adjustment,
                "adjustment_fallback": False,
                "source_document_id": row["source_document_id"],
                "data_quality": "ok" if base and latest else "missing_price",
            }
        )

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO analytics.asset_event_return (
              asset_id, stock_code, stock_name, event_type, event_date,
              base_trade_date, latest_trade_date, base_close, latest_close,
              return_pct, price_source, adjustment, adjustment_fallback,
              source_document_id, data_quality, updated_at
            )
            VALUES (
              %(asset_id)s, %(stock_code)s, %(stock_name)s, 'financial_report', %(event_date)s,
              %(base_trade_date)s, %(latest_trade_date)s, %(base_close)s, %(latest_close)s,
              %(return_pct)s, %(price_source)s, %(adjustment)s, %(adjustment_fallback)s,
              %(source_document_id)s, %(data_quality)s, now()
            )
            ON CONFLICT (
              stock_code, event_type, event_date,
              (COALESCE(source_document_id, '00000000-0000-0000-0000-000000000000'::uuid)),
              price_source
            )
            DO UPDATE SET
              asset_id = EXCLUDED.asset_id,
              stock_name = EXCLUDED.stock_name,
              base_trade_date = EXCLUDED.base_trade_date,
              latest_trade_date = EXCLUDED.latest_trade_date,
              base_close = EXCLUDED.base_close,
              latest_close = EXCLUDED.latest_close,
              return_pct = EXCLUDED.return_pct,
              adjustment = EXCLUDED.adjustment,
              adjustment_fallback = EXCLUDED.adjustment_fallback,
              data_quality = EXCLUDED.data_quality,
              updated_at = now()
            """,
            upsert_rows,
        )
    return len(upsert_rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default="2026Q1")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--latest-date")
    parser.add_argument("--price-adjustment", choices=["qfq", "none"], default="qfq")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-metrics", action="store_true", help="Skip text extraction and only rebuild event returns from existing metrics.")
    args = parser.parse_args()

    year, quarter, period = report_period_from_arg(args.period)
    if quarter != 1:
        raise ValueError("initial extractor only supports Q1 wording")
    start_date = args.start_date or f"{year}-04-01"
    end_date = args.end_date or f"{year}-05-31"

    query = """
      SELECT r.document_id, r.stock_code, r.stock_name, r.report_date, r.report_title,
             a.asset_id,
             coalesce(d.content_text, d.content_md, d.raw_text, '') AS body
      FROM research.reports_resolved r
      JOIN corpus.documents d ON d.id = r.document_id
      LEFT JOIN market.assets a ON a.asset_type = 'stock' AND a.symbol = r.stock_code
      WHERE r.stock_code IS NOT NULL
        AND r.report_date BETWEEN %(start_date)s AND %(end_date)s
        AND (
          r.report_title ILIKE '%%一季%%'
          OR r.report_title ILIKE '%%Q1%%'
          OR r.report_title ILIKE '%%季度%%'
          OR d.title ILIKE '%%一季%%'
          OR d.title ILIKE '%%Q1%%'
        )
      ORDER BY r.report_date, r.stock_code, r.document_id
    """
    params = {"start_date": start_date, "end_date": end_date}
    if args.limit > 0:
        query += " LIMIT %(limit)s"
        params["limit"] = args.limit

    extracted = 0
    docs = 0
    with connect(autocommit=True) as conn:
        if not args.skip_metrics:
            if not args.dry_run:
                conn.execute(
                    """
                    DELETE FROM company.financial_statement_metrics
                    WHERE report_period = %(period)s
                      AND extraction_method = 'research_report_regex'
                      AND source = 'research.reports'
                    """,
                    {"period": period},
                )
            metric_rows = []
            rows = conn.execute(query, params).fetchall()
            for row in rows:
                docs += 1
                metrics = extract_metrics(row["body"])
                for metric in metrics:
                    extracted += 1
                    metric_rows.append(
                        {
                            "asset_id": row["asset_id"],
                            "stock_code": row["stock_code"],
                            "stock_name": row["stock_name"],
                            "report_period": period,
                            "report_date": row["report_date"],
                            "metric_name": metric.metric_name,
                            "metric_value": metric.metric_value,
                            "metric_yoy": metric.metric_yoy,
                            "source_document_id": row["document_id"],
                            "source_text": metric.source_text,
                            "confidence": metric.confidence,
                            "scope": metric.scope,
                            "report_title": row["report_title"],
                        }
                    )
            if metric_rows and not args.dry_run:
                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO company.financial_statement_metrics (
                          asset_id, stock_code, stock_name, report_period, report_type, report_date,
                          metric_name, metric_value, metric_yoy, source_document_id,
                          source_text, confidence, metadata, updated_at
                        )
                        VALUES (
                          %(asset_id)s, %(stock_code)s, %(stock_name)s, %(report_period)s, 'quarterly', %(report_date)s,
                          %(metric_name)s, %(metric_value)s, %(metric_yoy)s, %(source_document_id)s,
                          %(source_text)s, %(confidence)s,
                          jsonb_build_object(
                            'report_title', %(report_title)s::text,
                            'scope', %(scope)s::text
                          ), now()
                        )
                        ON CONFLICT (
                          stock_code, report_period, metric_name,
                          (COALESCE(source_document_id, '00000000-0000-0000-0000-000000000000'::uuid))
                        )
                        DO UPDATE SET
                          asset_id = EXCLUDED.asset_id,
                          stock_name = EXCLUDED.stock_name,
                          report_date = EXCLUDED.report_date,
                          metric_value = EXCLUDED.metric_value,
                          metric_yoy = EXCLUDED.metric_yoy,
                          source_text = EXCLUDED.source_text,
                          confidence = EXCLUDED.confidence,
                          metadata = EXCLUDED.metadata,
                          updated_at = now()
                        """,
                        metric_rows,
                    )
        event_returns = 0 if args.dry_run else upsert_event_returns(conn, period, args.latest_date, args.price_adjustment)

    print(f"financial_statement_metrics: docs={docs}, extracted={extracted}, event_returns={event_returns}, dry_run={args.dry_run}, skip_metrics={args.skip_metrics}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
