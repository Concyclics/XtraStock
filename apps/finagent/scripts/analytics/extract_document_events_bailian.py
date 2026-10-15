# -*- coding: utf-8 -*-
"""Extract structured events with Alibaba Cloud Bailian OpenAI-compatible API."""
from __future__ import annotations

from xtrastock.config import bootstrap_environment
bootstrap_environment()


import argparse
import hashlib
import json
import os
import re
import time
from typing import Any

import requests

from common import connect, jsonb, parse_source_types


PROMPT_VERSION = "finance_event_extraction_v1"
DEFAULT_BASE_URL = os.getenv("FINANCE_API_BASE_URL", "http://localhost:8082/v1")


SYSTEM_PROMPT = """你是金融文本事件抽取器。只输出 JSON，不输出解释。
从中文研报和新闻中抽取会影响 A 股公司、行业或指数的结构化事件。
event_type 只能使用：policy_support, government_investment, subsidy, demand_growth, price_hike,
order_contract, earnings_positive, rating_upgrade, rating_downgrade, regulation_risk,
demand_decline, price_cut, external_risk, other。
polarity 只能是 -1, 0, 1。impact_score 和 confidence 是 0 到 1 的数字。
stock_codes 使用 6 位 A 股代码；industries 使用中文行业/板块名；keywords 使用短词。
最多输出 5 个 events 和 12 个 keywords。"""


def strip_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    return json.loads(text)


def event_hash(document_id: str, event: dict[str, Any], method: str) -> str:
    payload = {
        "document_id": document_id,
        "event_type": event.get("event_type"),
        "evidence": str(event.get("evidence", ""))[:240],
        "summary": str(event.get("summary", ""))[:160],
        "method": method,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def call_bailian(base_url: str, api_key: str, model: str, title: str, text: str, timeout: int) -> dict[str, Any]:
    api_mode = (os.getenv("BAILIAN_API_MODE") or os.getenv("FINANCE_API_MODE") or "chat_completions").strip().lower()
    responses_mode = api_mode in {"responses", "response"}
    url = base_url.rstrip("/") + ("/responses" if responses_mode else "/chat/completions")
    user_content = json.dumps({"title": title, "text": text[:12000]}, ensure_ascii=False)
    if responses_mode:
        request_body = {
            "model": model,
            "input": f"[SYSTEM]\n{SYSTEM_PROMPT}\n\n[USER]\n{user_content}",
        }
    else:
        request_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": 1600,
        }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    retries = max(0, int(os.getenv("BAILIAN_RETRIES", "3")))
    resp = None
    for attempt in range(retries + 1):
        resp = requests.post(url, headers=headers, json=request_body, timeout=timeout)
        if resp.status_code not in {408, 409, 425, 429, 500, 502, 503, 504}:
            break
        if attempt < retries:
            time.sleep(min(5 * (2 ** attempt), 30))
    assert resp is not None
    resp.raise_for_status()
    data = resp.json()
    if responses_mode:
        response_text = "".join(
            part.get("text", "")
            for item in data.get("output") or []
            if item.get("type") == "message"
            for part in item.get("content") or []
            if part.get("type") in {"output_text", "text"}
        )
    else:
        response_text = data["choices"][0]["message"]["content"]
    parsed = strip_json(response_text)
    return {"raw": data, "parsed": parsed}


def normalize_list(value: Any, max_items: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:max_items]:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return out


def clamp_float(value: Any, default: float | None = None) -> float | None:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def clamp_polarity(value: Any) -> int:
    try:
        ivalue = int(value)
    except Exception:
        return 0
    return -1 if ivalue < 0 else 1 if ivalue > 0 else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-types", default="research_report,peoples_daily,news")
    parser.add_argument("--since", help="Inclusive published/report date, YYYY-MM-DD.")
    parser.add_argument("--until", help="Inclusive published/report date, YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--model", default=os.getenv("BAILIAN_MODEL_FAST", "qwen3.6-flash"))
    parser.add_argument("--base-url", default=os.getenv("BAILIAN_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args()

    key_env = os.getenv("BAILIAN_API_KEY_ENV", "BAILIAN_API_KEY")
    api_key = os.getenv(key_env)
    if not api_key:
        raise SystemExit(f"model API key is not set: {key_env}")

    method = f"bailian:{args.model}:{PROMPT_VERSION}"
    source_types = parse_source_types(args.source_types)
    run_id = None
    ok = failed = 0

    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai.extraction_runs (model_name, prompt_version, task_type, status, config)
                VALUES (%s, %s, 'document_event_extraction', 'running', %s)
                RETURNING id
                """,
                (args.model, PROMPT_VERSION, jsonb({"source_types": source_types, "since": args.since, "until": args.until})),
            )
            run_id = cur.fetchone()["id"]
            conn.commit()
            cur.execute(
                """
                SELECT
                  d.id::text AS document_id,
                  d.source_type,
                  d.source_name,
                  d.title,
                  COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) AS event_date,
                  concat_ws(E'\n', d.content_text, d.content_md, d.raw_text) AS body
                FROM corpus.documents d
                LEFT JOIN research.reports_resolved r ON r.document_id = d.id
                WHERE (%(source_types)s::text[] IS NULL OR d.source_type = ANY(%(source_types)s))
                  AND length(concat_ws(E'\n', d.content_text, d.content_md, d.raw_text)) > 80
                  AND (%(since)s::date IS NULL OR COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) >= %(since)s::date)
                  AND (%(until)s::date IS NULL OR COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) <= %(until)s::date)
                  AND (
                    %(force)s
                    OR NOT EXISTS (
                      SELECT 1 FROM ai.document_extractions x
                      WHERE x.document_id = d.id
                        AND x.extraction_schema = %(schema)s
                        AND x.status = 'ok'
                    )
                  )
                ORDER BY COALESCE(r.report_date, (d.published_at AT TIME ZONE 'Asia/Shanghai')::date) DESC NULLS LAST, d.id
                LIMIT %(limit)s
                """,
                {
                    "source_types": source_types,
                    "since": args.since,
                    "until": args.until,
                    "force": args.force,
                    "schema": PROMPT_VERSION,
                    "limit": args.limit,
                },
            )
            rows = cur.fetchall()
            conn.commit()

            for row in rows:
                try:
                    result = call_bailian(args.base_url, api_key, args.model, row.get("title") or "", row.get("body") or "", args.timeout)
                    parsed = result["parsed"]
                    cur.execute(
                        """
                        INSERT INTO ai.document_extractions (
                          run_id, document_id, extraction_schema, raw_response, parsed_result, confidence, status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, 'ok')
                        ON CONFLICT (run_id, document_id, extraction_schema) DO UPDATE SET
                          raw_response = EXCLUDED.raw_response,
                          parsed_result = EXCLUDED.parsed_result,
                          confidence = EXCLUDED.confidence,
                          status = 'ok',
                          error = NULL
                        """,
                        (
                            run_id,
                            row["document_id"],
                            PROMPT_VERSION,
                            jsonb(result["raw"]),
                            jsonb(parsed),
                            clamp_float(parsed.get("confidence"), 0.7),
                        ),
                    )
                    for kw in normalize_list(parsed.get("keywords"), 12):
                        cur.execute(
                            """
                            INSERT INTO corpus.document_keywords (document_id, keyword, weight, method)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (document_id, keyword, method) DO UPDATE
                            SET weight = EXCLUDED.weight
                            """,
                            (row["document_id"], kw, 0.8, method),
                        )
                    for event in parsed.get("events") or []:
                        if not isinstance(event, dict) or not event.get("event_type"):
                            continue
                        cur.execute(
                            """
                            INSERT INTO analytics.document_events (
                              document_id, event_date, event_type, polarity, impact_score, confidence,
                              entity_texts, stock_codes, industries, keywords, summary, evidence,
                              extraction_method, extraction_run_id, event_hash, metadata, updated_at
                            )
                            VALUES (
                              %(document_id)s, %(event_date)s, %(event_type)s, %(polarity)s,
                              %(impact_score)s, %(confidence)s, %(entity_texts)s, %(stock_codes)s,
                              %(industries)s, %(keywords)s, %(summary)s, %(evidence)s, %(method)s,
                              %(run_id)s, %(event_hash)s, %(metadata)s, now()
                            )
                            ON CONFLICT (event_hash) DO UPDATE SET
                              impact_score = EXCLUDED.impact_score,
                              confidence = EXCLUDED.confidence,
                              entity_texts = EXCLUDED.entity_texts,
                              stock_codes = EXCLUDED.stock_codes,
                              industries = EXCLUDED.industries,
                              keywords = EXCLUDED.keywords,
                              summary = EXCLUDED.summary,
                              evidence = EXCLUDED.evidence,
                              extraction_run_id = EXCLUDED.extraction_run_id,
                              metadata = EXCLUDED.metadata,
                              updated_at = now()
                            """,
                            {
                                "document_id": row["document_id"],
                                "event_date": row["event_date"],
                                "event_type": str(event.get("event_type")),
                                "polarity": clamp_polarity(event.get("polarity")),
                                "impact_score": clamp_float(event.get("impact_score"), 0.5),
                                "confidence": clamp_float(event.get("confidence"), 0.7),
                                "entity_texts": normalize_list(event.get("entity_texts")),
                                "stock_codes": normalize_list(event.get("stock_codes")),
                                "industries": normalize_list(event.get("industries")),
                                "keywords": normalize_list(event.get("keywords")),
                                "summary": str(event.get("summary") or "")[:800],
                                "evidence": str(event.get("evidence") or "")[:1200],
                                "method": method,
                                "run_id": run_id,
                                "event_hash": event_hash(row["document_id"], event, method),
                                "metadata": jsonb({"model": args.model, "source_type": row.get("source_type")}),
                            },
                        )
                    ok += 1
                    conn.commit()
                except Exception as exc:
                    failed += 1
                    conn.rollback()
                    with conn.cursor() as fail_cur:
                        fail_cur.execute(
                            """
                            INSERT INTO ai.document_extractions (
                              run_id, document_id, extraction_schema, status, error
                            )
                            VALUES (%s, %s, %s, 'failed', %s)
                            ON CONFLICT (run_id, document_id, extraction_schema) DO UPDATE SET
                              status = 'failed',
                              error = EXCLUDED.error
                            """,
                            (run_id, row["document_id"], PROMPT_VERSION, str(exc)[:1000]),
                        )
                        fail_cur.execute(
                            "UPDATE ai.extraction_runs SET status = 'partial' WHERE id = %s",
                            (run_id,),
                        )
                        conn.commit()
                if args.sleep:
                    time.sleep(args.sleep)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ai.extraction_runs
                    SET status = %s, finished_at = now()
                    WHERE id = %s
                    """,
                    ("ok" if failed == 0 else "partial", run_id),
                )
                conn.commit()

    print(f"run_id={run_id} ok={ok} failed={failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
