#!/usr/bin/env python3
"""Process ranked temporal extraction candidates through a pluggable LLM."""
from __future__ import annotations

import argparse
import hashlib
import json
import time

from common import connect, jsonb
from extract_document_events_bailian import clamp_float, clamp_polarity, normalize_list
from llm_client import get_llm_client


PROMPT_VERSION = "temporal_finance_extraction_v1"
SYSTEM = """你是金融事实抽取器，只输出JSON。事实必须能由原文证据直接支持，不得补全公司、数值或关系。
event任务输出events数组；每项包含event_type, polarity(-1/0/1), impact_score, confidence,
stock_codes, industries, keywords, summary, evidence。
supply_chain任务输出relations数组；每项包含source_name,target_name,relation_type,
stock_code,confidence,evidence,valid_from。relation_type只能是PRODUCES,USES,SUPPLIES_TO,
UPSTREAM_OF,DOWNSTREAM_OF,USED_IN,COMPONENT_OF,EXPOSED_TO。
forecast_rating任务输出rating对象和forecasts数组；数值缺失必须为null，禁止推算。
最多5个事实；evidence必须是原文短片段。"""


def event_hash(document_id: str, event: dict, method: str) -> str:
    raw = json.dumps({"document_id": document_id, "event": event, "method": method}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def number(value):
    try:
        result = float(value)
        return result if result == result and abs(result) < 1e18 else None
    except (TypeError, ValueError):
        return None


def forecast_year(value):
    try:
        year = int(value)
        return year if 2000 <= year <= 2100 else None
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider")
    parser.add_argument("--task-types", default="event,supply_chain,forecast_rating")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--model")
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args()
    task_types = [x.strip() for x in args.task_types.split(",") if x.strip()]
    client = get_llm_client(args.provider)
    method = f"llm:{PROMPT_VERSION}"

    with connect(autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO ai.extraction_runs(model_name,prompt_version,task_type,status,config)
                   VALUES (%s,%s,'temporal_candidate_extraction','running',%s) RETURNING id""",
                (args.model or "provider-default", PROMPT_VERSION, jsonb({"task_types": task_types})),
            )
            run_id = cur.fetchone()["id"]
            cur.execute(
                """
                WITH picked AS MATERIALIZED (
                  SELECT c.candidate_id,c.document_id,c.task_type,c.candidate_date
                  FROM kg.extraction_candidates c
                  WHERE c.status IN ('queued','failed') AND c.task_type=ANY(%s)
                  ORDER BY c.priority_score DESC,c.candidate_date DESC
                  LIMIT %s FOR UPDATE OF c SKIP LOCKED
                )
                SELECT c.candidate_id,c.document_id::text,c.task_type,c.candidate_date,
                       d.title,d.source_type,d.source_name,
                       left(concat_ws(E'\n',d.content_text,d.content_md,d.raw_text),12000) AS body
                FROM picked c JOIN corpus.documents d ON d.id=c.document_id
                """,
                (task_types, max(1, min(args.limit, 1000))),
            )
            rows = cur.fetchall()
            candidate_ids = [row["candidate_id"] for row in rows]
            if candidate_ids:
                cur.execute(
                    "UPDATE kg.extraction_candidates SET status='running',claimed_at=now(),attempt_count=attempt_count+1 WHERE candidate_id=ANY(%s)",
                    (candidate_ids,),
                )
            conn.commit()

        ok = failed = 0
        for row in rows:
            try:
                payload = client.chat_json([
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": json.dumps({
                        "task": row["task_type"], "date": str(row["candidate_date"]),
                        "title": row.get("title"), "source": row.get("source_name"),
                        "text": row.get("body") or "",
                    }, ensure_ascii=False)},
                ], PROMPT_VERSION, args.model)
                parsed = payload["parsed"]
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO ai.document_extractions
                             (run_id,document_id,extraction_schema,raw_response,parsed_result,confidence,status)
                           VALUES (%s,%s,%s,%s,%s,%s,'ok')""",
                        (run_id,row["document_id"],f"{PROMPT_VERSION}:{row['task_type']}",
                         jsonb(payload.get("raw_response")),jsonb(parsed),clamp_float(parsed.get("confidence"),0.7)),
                    )
                    if row["task_type"] == "event":
                        for event in (parsed.get("events") or [])[:5]:
                            if not isinstance(event,dict) or not event.get("event_type") or not event.get("evidence"):
                                continue
                            cur.execute(
                                """INSERT INTO analytics.document_events
                                   (document_id,event_date,event_type,polarity,impact_score,confidence,
                                    entity_texts,stock_codes,industries,keywords,summary,evidence,
                                    extraction_method,extraction_run_id,event_hash,metadata,updated_at)
                                   VALUES (%(document_id)s,%(event_date)s,%(event_type)s,%(polarity)s,%(impact)s,%(confidence)s,
                                    %(entities)s,%(codes)s,%(industries)s,%(keywords)s,%(summary)s,%(evidence)s,
                                    %(method)s,%(run_id)s,%(hash)s,%(metadata)s,now())
                                   ON CONFLICT(event_hash) DO UPDATE SET confidence=EXCLUDED.confidence,
                                     summary=EXCLUDED.summary,evidence=EXCLUDED.evidence,updated_at=now()""",
                                {
                                    "document_id":row["document_id"],"event_date":row["candidate_date"],
                                    "event_type":str(event["event_type"])[:80],"polarity":clamp_polarity(event.get("polarity")),
                                    "impact":clamp_float(event.get("impact_score"),0.5),
                                    "confidence":clamp_float(event.get("confidence"),0.7),
                                    "entities":normalize_list(event.get("entity_texts")),
                                    "codes":normalize_list(event.get("stock_codes")),
                                    "industries":normalize_list(event.get("industries")),
                                    "keywords":normalize_list(event.get("keywords")),
                                    "summary":str(event.get("summary") or "")[:800],
                                    "evidence":str(event.get("evidence") or "")[:1200],"method":method,"run_id":run_id,
                                    "hash":event_hash(row["document_id"],event,method),
                                    "metadata":jsonb({"provider":payload.get("provider"),"model":payload.get("model")}),
                                },
                            )
                    elif row["task_type"] == "forecast_rating":
                        report = cur.execute(
                            "SELECT stock_code FROM research.reports_resolved WHERE document_id=%s",
                            (row["document_id"],),
                        ).fetchone() or {}
                        rating = parsed.get("rating") if isinstance(parsed.get("rating"),dict) else {}
                        target_price = number(rating.get("target_price"))
                        if any((rating.get("rating"),rating.get("rating_change"),rating.get("previous_rating"),target_price is not None)):
                            cur.execute(
                                """INSERT INTO research.report_ratings
                                     (document_id,stock_code,rating,rating_change,previous_rating,target_price,
                                      extracted_by,confidence,metadata)
                                   SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s
                                   WHERE NOT EXISTS (
                                     SELECT 1 FROM research.report_ratings WHERE document_id=%s AND extracted_by=%s
                                   )""",
                                (row["document_id"],report.get("stock_code"),str(rating.get("rating") or "")[:80] or None,
                                 str(rating.get("rating_change") or "")[:80] or None,
                                 str(rating.get("previous_rating") or "")[:80] or None,target_price,method,
                                 clamp_float(rating.get("confidence"),0.7),
                                 jsonb({"evidence":str(rating.get("evidence") or "")[:1200],"run_id":str(run_id)}),
                                 row["document_id"],method),
                            )
                        for item in (parsed.get("forecasts") or [])[:8]:
                            if not isinstance(item,dict):
                                continue
                            year = forecast_year(item.get("forecast_year"))
                            values = {key:number(item.get(key)) for key in ("revenue","net_profit","eps","pe")}
                            if year is None or not any(value is not None for value in values.values()):
                                continue
                            cur.execute(
                                """INSERT INTO research.report_forecasts
                                     (document_id,forecast_year,revenue,net_profit,eps,pe,currency,source,confidence,metadata)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                   ON CONFLICT(document_id,forecast_year,source) DO UPDATE SET
                                     revenue=EXCLUDED.revenue,net_profit=EXCLUDED.net_profit,eps=EXCLUDED.eps,
                                     pe=EXCLUDED.pe,confidence=EXCLUDED.confidence,metadata=EXCLUDED.metadata""",
                                (row["document_id"],year,values["revenue"],values["net_profit"],values["eps"],values["pe"],
                                 str(item.get("currency") or "CNY")[:12],method,
                                 clamp_float(item.get("confidence"),0.7),
                                 jsonb({"evidence":str(item.get("evidence") or "")[:1200],"run_id":str(run_id)})),
                            )
                    elif row["task_type"] == "supply_chain":
                        for item in (parsed.get("relations") or [])[:8]:
                            if not isinstance(item,dict) or not item.get("target_name") or not item.get("evidence"):
                                continue
                            stock_code = str(item.get("stock_code") or "").strip()
                            asset = cur.execute(
                                "SELECT asset_id,name FROM market.assets WHERE asset_type='stock' AND symbol=%s LIMIT 1",
                                (stock_code,),
                            ).fetchone()
                            if not asset:
                                continue
                            target_name = str(item["target_name"]).strip()[:160]
                            relation_type = str(item.get("relation_type") or "EXPOSED_TO").upper()
                            concept_type = "material" if any(x in target_name for x in ("粉","气","材","金属","化物","基板")) else "subsegment"
                            concept = cur.execute(
                                """INSERT INTO kg.concepts
                                     (concept_type,name,normalized_name,description,ai_hardware_relevance,status,source,metadata)
                                   VALUES (%s,%s,lower(%s),%s,%s,'candidate',%s,%s)
                                   ON CONFLICT(concept_type,normalized_name) DO UPDATE SET updated_at=now()
                                   RETURNING concept_id""",
                                (concept_type,target_name,target_name,
                                 "LLM extracted candidate; requires evidence review.",
                                 clamp_float(item.get("confidence"),0.6),method,
                                 jsonb({"run_id":str(run_id),"document_id":row["document_id"]})),
                            ).fetchone()
                            exposure_type = "direct_product" if relation_type=="PRODUCES" else "weak_mention"
                            cur.execute(
                                """INSERT INTO kg.company_concept_exposures
                                     (asset_id,stock_code,stock_name,concept_id,relation_type,evidence_document_id,
                                      evidence_date,evidence,confidence,source,status,business_role,directness,
                                      evidence_span,extractor_version,review_status,metadata)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'candidate',%s,'unknown',%s,%s,'unreviewed',%s)
                                   ON CONFLICT DO NOTHING""",
                                (asset["asset_id"],stock_code,asset.get("name"),concept["concept_id"],exposure_type,
                                 row["document_id"],row["candidate_date"],str(item["evidence"])[:1200],
                                 clamp_float(item.get("confidence"),0.6),method,relation_type,
                                 str(item["evidence"])[:1200],method,
                                 jsonb({"run_id":str(run_id),"llm_candidate":True})),
                            )
                    cur.execute(
                        """UPDATE kg.extraction_candidates SET status='complete',completed_at=now(),last_error=NULL,updated_at=now()
                           WHERE candidate_id=%s""",
                        (row["candidate_id"],),
                    )
                    cur.execute(
                        """INSERT INTO kg.document_extraction_status
                             (document_id,extraction_method,status,processed_at,source_updated_at,details)
                           SELECT %s,%s,'ok',now(),updated_at,%s FROM corpus.documents WHERE id=%s
                           ON CONFLICT(document_id,extraction_method) DO UPDATE SET status='ok',processed_at=now(),
                             source_updated_at=EXCLUDED.source_updated_at,details=EXCLUDED.details""",
                        (row["document_id"],method,jsonb({"task_type":row["task_type"],"run_id":str(run_id)}),row["document_id"]),
                    )
                conn.commit()
                ok += 1
            except Exception as exc:
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE kg.extraction_candidates SET status='failed',last_error=%s,updated_at=now() WHERE candidate_id=%s",
                        (str(exc)[:1500],row["candidate_id"]),
                    )
                    cur.execute("UPDATE ai.extraction_runs SET status='partial' WHERE id=%s",(run_id,))
                conn.commit()
                failed += 1
            if args.sleep:
                time.sleep(args.sleep)
        with conn.cursor() as cur:
            cur.execute("UPDATE ai.extraction_runs SET status=%s,finished_at=now() WHERE id=%s",
                        ("ok" if failed==0 else "partial",run_id))
        conn.commit()
    print(f"run_id={run_id} selected={len(rows)} ok={ok} failed={failed}")
    return 0 if failed==0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
