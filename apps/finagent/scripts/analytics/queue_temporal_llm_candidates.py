#!/usr/bin/env python3
"""Rank ambiguous/high-value documents for provider-neutral LLM extraction."""
from __future__ import annotations

import argparse
import datetime as dt

from common import connect


SQL = """
WITH recent_ids AS MATERIALIZED (
 SELECT document_id AS id FROM research.reports WHERE report_date BETWEEN %(since)s AND %(until)s
 UNION
 SELECT id FROM corpus.documents
 WHERE published_at >= (%(since)s::date::timestamp AT TIME ZONE 'Asia/Shanghai')
   AND published_at < ((%(until)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
), dated AS MATERIALIZED (
  SELECT d.id,d.source_type,d.source_name,d.title,
         coalesce(r.report_date,(d.published_at AT TIME ZONE 'Asia/Shanghai')::date,d.created_at::date) AS dt,
         r.report_type,r.stock_code,r.industry_name
  FROM recent_ids ids JOIN corpus.documents d ON d.id=ids.id
  LEFT JOIN research.reports_resolved r ON r.document_id=d.id
  WHERE (r.report_date BETWEEN %(since)s AND %(until)s
         OR d.published_at >= (%(since)s::date::timestamp AT TIME ZONE 'Asia/Shanghai')
            AND d.published_at < ((%(until)s::date+1)::timestamp AT TIME ZONE 'Asia/Shanghai'))
    AND coalesce(r.report_date,(d.published_at AT TIME ZONE 'Asia/Shanghai')::date,d.created_at::date)
        BETWEEN %(since)s AND %(until)s
), docs AS MATERIALIZED (
 SELECT x.*,concat_ws(E'\n',x.title,d.content_text,d.content_md,d.raw_text) AS body
 FROM dated x JOIN corpus.documents d ON d.id=x.id
 WHERE length(concat_ws(E'\n',d.content_text,d.content_md,d.raw_text))>=120
), scored AS MATERIALIZED (
  SELECT *,
    CASE source_name WHEN 'peoples_daily' THEN 1.0 WHEN 'xinhua' THEN .95
      WHEN 'eastmoney' THEN .75 ELSE .55 END AS authority,
    (CASE WHEN body ~* '(AI|人工智能|算力|GPU|服务器|数据中心|光模块|CPO|HBM|先进封装|液冷|PCB|玻璃基板|半导体|芯片)' THEN 1 ELSE 0 END
     + CASE WHEN body ~ '(材料|设备|部件|供应商|客户|产能|良率|替代|国产化)' THEN .5 ELSE 0 END) AS ai_score,
    (CASE WHEN body ~ '(预计|预测|目标价|评级|上调|下调|订单|中标|政策|投资|涨价|降价)' THEN .8 ELSE .2 END
     + CASE WHEN stock_code IS NULL AND body ~ '[0368][0-9]{5}' THEN .3 ELSE 0 END) AS ambiguity,
    ARRAY_REMOVE(ARRAY[
      CASE WHEN body ~* '(AI|人工智能|算力|GPU|服务器|数据中心|光模块|CPO|HBM|先进封装|液冷|PCB|玻璃基板)' THEN 'ai_hardware' END,
      CASE WHEN body ~ '(材料|设备|部件|供应商|客户|产能|良率|替代|国产化)' THEN 'supply_chain' END,
      CASE WHEN body ~ '(预计|预测|目标价|评级|上调|下调)' THEN 'forecast_or_rating' END,
      CASE WHEN body ~ '(订单|中标|政策|投资|涨价|降价|风险)' THEN 'market_event' END
    ],NULL) AS reasons
  FROM docs
), candidates AS (
  SELECT *,authority*.25+least(ai_score,1.5)*.45+least(ambiguity,1.1)*.30 AS priority
  FROM scored WHERE ai_score>0 OR ambiguity>=.8
), tasks AS (
  SELECT *,unnest(ARRAY_REMOVE(ARRAY[
    CASE WHEN 'market_event'=ANY(reasons) THEN 'event' END,
    CASE WHEN 'supply_chain'=ANY(reasons) OR 'ai_hardware'=ANY(reasons) THEN 'supply_chain' END,
    CASE WHEN 'forecast_or_rating'=ANY(reasons) AND source_type='research_report' THEN 'forecast_rating' END
  ],NULL)) AS task_type
  FROM candidates ORDER BY priority DESC,dt DESC LIMIT %(limit)s
)
INSERT INTO kg.extraction_candidates
  (document_id,task_type,candidate_date,priority_score,ai_hardware_score,source_authority_score,
   ambiguity_score,reasons,status,extractor_version,metadata,updated_at)
SELECT id,task_type,dt,priority,least(ai_score/1.5,1),authority,least(ambiguity/1.1,1),reasons,
       'queued','temporal_candidate_ranker:v1',
       jsonb_build_object('source_type',source_type,'source_name',source_name,'title',title,
                          'stock_code',stock_code,'industry_name',industry_name),now()
FROM tasks
ON CONFLICT (document_id,task_type,extractor_version) DO UPDATE SET
  priority_score=EXCLUDED.priority_score,ai_hardware_score=EXCLUDED.ai_hardware_score,
  source_authority_score=EXCLUDED.source_authority_score,ambiguity_score=EXCLUDED.ambiguity_score,
  reasons=EXCLUDED.reasons,metadata=EXCLUDED.metadata,updated_at=now()
WHERE (kg.extraction_candidates.priority_score,kg.extraction_candidates.ai_hardware_score,
       kg.extraction_candidates.source_authority_score,kg.extraction_candidates.ambiguity_score,
       kg.extraction_candidates.reasons,kg.extraction_candidates.metadata)
 IS DISTINCT FROM (EXCLUDED.priority_score,EXCLUDED.ai_hardware_score,
       EXCLUDED.source_authority_score,EXCLUDED.ambiguity_score,EXCLUDED.reasons,EXCLUDED.metadata)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--limit", type=int, default=20000)
    args = parser.parse_args()
    since, until = dt.date.fromisoformat(args.since), dt.date.fromisoformat(args.until)
    with connect(autocommit=True) as conn:
        conn.execute("SET statement_timeout='60s'")
        with conn.cursor() as cur:
            cur.execute(SQL, {"since": since, "until": until, "limit": max(1, min(args.limit, 100000))})
            print(f"queued_or_refreshed={cur.rowcount} since={since} until={until}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
