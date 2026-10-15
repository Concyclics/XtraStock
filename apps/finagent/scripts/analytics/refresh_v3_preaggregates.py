#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/pipeline"))

from common import connect  # noqa: E402
from v3_ops import set_dataset_watermark, stage_run  # noqa: E402
from market_data_date import complete_market_date  # noqa: E402


def upstream_quality(rows, latest, expected, upstream_status):
    if not rows:
        return 'fail'
    return 'pass' if latest and expected and latest >= expected and upstream_status == 'ok' else 'warn'



def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh small Agent V3 factor/graph preaggregations.")
    parser.add_argument("--as-of", default=dt.date.today().isoformat())
    args = parser.parse_args()
    as_of = dt.date.fromisoformat(args.as_of)

    with stage_run("finance_v3_analytics", "refresh_preaggregates", expected_date=as_of) as run:
        with connect(admin=False) as conn:
            conn.execute("SET LOCAL statement_timeout='90s'")
            conn.execute("SET LOCAL enable_nestloop=off")
            market_date,market_status=complete_market_date(conn,as_of)
            graph_run=conn.execute("SELECT status,end_date FROM ops.crawler_runs WHERE task_name='daily_knowledge_graph' AND end_date<=%s ORDER BY started_at DESC LIMIT 1",(as_of,)).fetchone() or {}
            graph_success_date=conn.execute("SELECT max(end_date) AS dt FROM ops.crawler_runs WHERE task_name='daily_knowledge_graph' AND status='ok' AND end_date<=%s",(as_of,)).fetchone()['dt']
            factor_run=conn.execute("SELECT status,end_date FROM ops.crawler_runs WHERE task_name='daily_factor_materialize' AND end_date<=%s ORDER BY started_at DESC LIMIT 1",(as_of,)).fetchone() or {}
            max_score = conn.execute(
                "SELECT max(dt) AS dt FROM factor.daily_scores WHERE dt <= %s", (as_of,)
            ).fetchone()["dt"]
            factor_rows = 0
            if max_score:
                conn.execute("DELETE FROM analytics.factor_score_latest")
                result = conn.execute(
                    """
                    INSERT INTO analytics.factor_score_latest (
                      factor_id,asset_id,dt,raw_value,percentile,zscore,lifecycle_status,
                      available_at,source_feature_date,refreshed_at
                    )
                    SELECT DISTINCT ON (s.factor_id,s.asset_id)
                           s.factor_id,s.asset_id,s.dt,s.raw_value,s.percentile,s.zscore,
                           d.lifecycle_status,s.available_at,s.source_feature_date,now()
                    FROM factor.valid_daily_scores_v1 s
                    JOIN factor.definitions d ON d.factor_id=s.factor_id
                    WHERE s.dt BETWEEN %s::date - 10 AND %s::date
                      AND d.lifecycle_status IN ('shadow','production')
                    ORDER BY s.factor_id,s.asset_id,s.dt DESC,s.available_at DESC
                    """,
                    (max_score, max_score),
                )
                factor_rows = result.rowcount

            conn.execute("DELETE FROM analytics.graph_adjacency_latest")
            graph_result = conn.execute(
                """
                INSERT INTO analytics.graph_adjacency_latest (
                  relation_version_id,relation_id,source_entity_id,target_entity_id,
                  relation_type,valid_from,valid_to,public_known_from,confidence,
                  review_status,claim_class,source_system,source_document_id,refreshed_at
                )
                SELECT rv.relation_version_id,r.relation_id,r.source_entity_id,r.target_entity_id,
                       r.relation_type,lower(rv.valid_during),upper(rv.valid_during),
                       lower(rv.public_known_during),rv.confidence,rv.review_status,
                       CASE
                         WHEN rv.review_status='approved' AND rv.source_system ~* '(cninfo|sse|szse|bse|official)'
                           THEN 'observed_fact'
                         WHEN rv.extractor_version ~* '(llm|model)' THEN 'model_inference'
                         ELSE 'extracted_claim'
                       END,
                       rv.source_system,ev.document_id,now()
                FROM kg.relation_versions rv
                JOIN kg.relations r ON r.relation_id=rv.relation_id
                LEFT JOIN LATERAL (
                  SELECT e.document_id
                  FROM kg.relation_version_evidence e
                  WHERE e.relation_version_id=rv.relation_version_id
                  ORDER BY e.public_at DESC NULLS LAST,e.created_at DESC LIMIT 1
                ) ev ON true
                WHERE rv.status='active' AND rv.valid_during @> %s::date
                  AND rv.public_known_during @> now()
                """,
                (as_of,),
            )
            graph_rows = graph_result.rowcount
            # Raw scores can be newer than the last valid feature-contract
            # version. Report freshness of the rows consumers actually read.
            max_score=conn.execute('SELECT max(dt) AS dt FROM analytics.factor_score_latest').fetchone()['dt']
            factor_date=min(max_score,factor_run['end_date']) if max_score and factor_run.get('end_date') else None
            factor_quality=upstream_quality(factor_rows,factor_date,market_date,factor_run.get('status'))
            graph_quality=upstream_quality(graph_rows,graph_run.get('end_date'),market_date,graph_run.get('status'))
            graph_details={'preaggregate':'analytics.graph_adjacency_latest','upstream_status':graph_run.get('status'),
                           'upstream_date':str(graph_run.get('end_date')),'last_success_date':str(graph_success_date),'expected_market_date':str(market_date),'market_date_gate':market_status}
            factor_details={'preaggregate':'analytics.factor_score_latest','upstream_status':factor_run.get('status'),
                            'upstream_date':str(factor_run.get('end_date')),
                            'expected_market_date':str(market_date),'valid_scores_only':True,'market_date_gate':market_status}


            conn.execute(
                """
                INSERT INTO analytics.dataset_coverage_latest (
                  dataset_key,partition_key,data_through,row_count,entity_count,
                  eligible_count,coverage_pct,quality_status,details,refreshed_at
                )
                SELECT 'factor.daily_scores','default',max(dt)::timestamp AT TIME ZONE 'Asia/Shanghai',
                       count(*),count(DISTINCT asset_id),NULL,NULL,
                       CASE WHEN count(*)>0 THEN 'pass' ELSE 'fail' END,
                       jsonb_build_object('preaggregate','analytics.factor_score_latest'),now()
                FROM analytics.factor_score_latest
                ON CONFLICT (dataset_key,partition_key) DO UPDATE SET
                  data_through=EXCLUDED.data_through,row_count=EXCLUDED.row_count,
                  entity_count=EXCLUDED.entity_count,quality_status=EXCLUDED.quality_status,
                  details=EXCLUDED.details,refreshed_at=now()
                """
            )
            conn.execute(
                """
                INSERT INTO analytics.dataset_coverage_latest (
                  dataset_key,partition_key,data_through,row_count,entity_count,
                  eligible_count,coverage_pct,quality_status,details,refreshed_at
                )
                SELECT 'kg.temporal_graph','default',max(public_known_from),count(*),
                       count(DISTINCT source_entity_id)+count(DISTINCT target_entity_id),NULL,NULL,
                       CASE WHEN count(*)>0 THEN 'pass' ELSE 'warn' END,
                       jsonb_build_object(
                         'preaggregate','analytics.graph_adjacency_latest',
                         'observed_fact',count(*) FILTER (WHERE claim_class='observed_fact'),
                         'extracted_claim',count(*) FILTER (WHERE claim_class='extracted_claim'),
                         'model_inference',count(*) FILTER (WHERE claim_class='model_inference')
                       ),now()
                FROM analytics.graph_adjacency_latest
                ON CONFLICT (dataset_key,partition_key) DO UPDATE SET
                  data_through=EXCLUDED.data_through,row_count=EXCLUDED.row_count,
                  entity_count=EXCLUDED.entity_count,quality_status=EXCLUDED.quality_status,
                  details=EXCLUDED.details,refreshed_at=now()
                """
            )

            for dataset,quality,details in [('factor.daily_scores',factor_quality,factor_details),('kg.temporal_graph',graph_quality,graph_details)]:
                conn.execute("UPDATE analytics.dataset_coverage_latest SET quality_status=%s,details=details||%s WHERE dataset_key=%s AND partition_key='default'",(quality,Jsonb(details),dataset))

        set_dataset_watermark(
            "factor.daily_scores", schema_version="v3", run_id=run["run_id"],
            expected_date=as_of,
            data_through=dt.datetime.combine(max_score, dt.time(23, 59), dt.timezone(dt.timedelta(hours=8))) if max_score else None,
            row_count=factor_rows, quality_status=factor_quality,
            source_version="factor_score_latest:v1",
            details=factor_details,
        )
        set_dataset_watermark(
            "kg.temporal_graph", schema_version="v3", run_id=run["run_id"],
            expected_date=as_of, data_through=dt.datetime.combine(graph_success_date,dt.time(23,59),dt.timezone(dt.timedelta(hours=8))) if graph_success_date else None,
            row_count=graph_rows, quality_status=graph_quality,
            source_version="graph_adjacency_latest:v1",
            details=graph_details,
        )
        run["quality_status"] = "pass" if factor_quality == graph_quality == "pass" else "warn"
        run["output_watermarks"] = {"factor_rows": factor_rows, "graph_rows": graph_rows}
    print({"factor_rows": factor_rows, "factor_date": max_score, "graph_rows": graph_rows})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
