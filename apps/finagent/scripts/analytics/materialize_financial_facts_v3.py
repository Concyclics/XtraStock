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
from v3_ops import record_quality, set_dataset_watermark, stage_run  # noqa: E402


METRICS = {
    "营业总收入": ("revenue", "营业总收入", "CNY"),
    "归母净利润": ("net_profit_parent", "归母净利润", "CNY"),
    "每股收益": ("eps", "每股收益", "CNY/share"),
    "净资产收益率": ("roe", "净资产收益率", "percentage_points"),
    "营收同比": ("revenue_yoy", "营业收入同比", "percentage_points"),
    "净利同比": ("net_profit_parent_yoy", "归母净利润同比", "percentage_points"),
}


def period_type(value: dt.date) -> str:
    if value.month == 12:
        return "annual"
    return "year_to_date"


def main() -> int:
    parser = argparse.ArgumentParser(description="Dual-write legacy actual/forecast rows into typed V3 financial facts.")
    parser.add_argument("--since", default="2020-01-01")
    parser.add_argument("--until", default=dt.date.today().isoformat())
    args = parser.parse_args()
    since = dt.date.fromisoformat(args.since)
    until = dt.date.fromisoformat(args.until)
    if since > until:
        parser.error("--since cannot be after --until")

    actual_rows = 0
    forecast_rows = 0
    with stage_run("finance_v3_analytics", "materialize_financial_facts", expected_date=until,
                   metadata={"since": since.isoformat(), "until": until.isoformat()}) as run:
        with connect(admin=False) as conn:
            actual_result = conn.execute(
                """
                INSERT INTO company.financial_facts (
                  filing_id,asset_id,stock_code,metric_code,metric_name,fact_kind,
                  statement_scope,period_type,period_end,value,yoy,unit,currency,
                  announced_at,available_at,revision_no,source_name,source_document_id,
                  source_text,confidence,quality_status,metadata
                )
                SELECT NULL,m.asset_id,a.symbol,
                       CASE m.metric_name
                         WHEN '营业总收入' THEN 'revenue'
                         WHEN '归母净利润' THEN 'net_profit_parent'
                         WHEN '每股收益' THEN 'eps'
                         WHEN '净资产收益率' THEN 'roe'
                         WHEN '营收同比' THEN 'revenue_yoy'
                         WHEN '净利同比' THEN 'net_profit_parent_yoy'
                       END,
                       m.metric_name,'actual','consolidated',
                       CASE WHEN m.report_date=make_date(extract(year from m.report_date)::int,12,31)
                            THEN 'annual' ELSE 'year_to_date' END,
                       m.report_date,m.metric_value,
                       CASE WHEN m.metric_name IN ('营收同比','净利同比') THEN m.metric_value END,
                       CASE WHEN m.metric_name IN ('营业总收入','归母净利润') THEN 'CNY'
                            WHEN m.metric_name='每股收益' THEN 'CNY/share'
                            ELSE 'percentage_points' END,
                       CASE WHEN m.metric_name IN ('营业总收入','归母净利润','每股收益') THEN 'CNY' END,
                       m.available_at,m.available_at,1,m.source,NULL,NULL,NULL,'unverified',
                       jsonb_build_object(
                         'legacy_table','company.financial_metrics',
                         'legacy_metric_name',m.metric_name,
                         'yoy_unit','percentage_points',
                         'raw_payload',m.raw_payload,
                         'official_source',false
                       )
                FROM company.financial_metrics m
                JOIN market.assets a ON a.asset_id=m.asset_id
                WHERE m.report_date BETWEEN %s AND %s AND m.metric_name=ANY(%s)
                ON CONFLICT (
                  filing_id,asset_id,metric_code,fact_kind,statement_scope,period_end,revision_no,source_name
                ) DO UPDATE SET
                  stock_code=EXCLUDED.stock_code,metric_name=EXCLUDED.metric_name,
                  value=EXCLUDED.value,yoy=EXCLUDED.yoy,unit=EXCLUDED.unit,
                  currency=EXCLUDED.currency,announced_at=EXCLUDED.announced_at,
                  available_at=EXCLUDED.available_at,quality_status=EXCLUDED.quality_status,
                  metadata=EXCLUDED.metadata
                """,
                (since, until, list(METRICS)),
            )
            actual_rows = actual_result.rowcount

            forecasts = conn.execute(
                """
                SELECT f.document_id,f.forecast_year,f.revenue,f.net_profit,f.eps,f.pe,
                       f.currency,f.source,f.confidence,f.metadata,f.created_at,
                       r.stock_code,r.institution,r.report_date,
                       greatest(coalesce(r.available_at,d.published_at,r.created_at),f.created_at,f.updated_at,r.identity_known_at) AS available_at,
                       coalesce(d.published_at,r.available_at,r.created_at) AS announced_at,
                       a.asset_id
                FROM research.report_forecasts_usable f
                JOIN research.reports_resolved r ON r.document_id=f.document_id
                JOIN corpus.documents d ON d.id=f.document_id
                LEFT JOIN market.assets a ON a.symbol=r.stock_code AND a.asset_type='stock'
                WHERE r.report_date BETWEEN %s AND %s
                ORDER BY r.report_date,f.id
                """,
                (since, until),
            ).fetchall()
            for row in forecasts:
                values = (
                    ("forecast_revenue", "预测营业收入", row["revenue"], "CNY"),
                    ("forecast_net_profit", "预测归母净利润", row["net_profit"], "CNY"),
                    ("forecast_eps", "预测每股收益", row["eps"], "CNY/share"),
                    ("forward_pe", "预测市盈率", row["pe"], "times"),
                )
                for metric_code, metric_name, value, unit in values:
                    if value is None:
                        continue
                    source_name = f"research_report:{row['source']}:{row['document_id']}"
                    conn.execute(
                        """
                        INSERT INTO company.financial_facts (
                          asset_id,stock_code,metric_code,metric_name,fact_kind,statement_scope,
                          period_type,period_end,value,unit,currency,announced_at,available_at,
                          revision_no,source_name,source_document_id,confidence,quality_status,metadata
                        ) VALUES (%s,%s,%s,%s,'forecast','unknown','annual',%s,%s,%s,%s,%s,%s,1,%s,%s,%s,'unverified',%s)
                        ON CONFLICT (
                          filing_id,asset_id,metric_code,fact_kind,statement_scope,period_end,revision_no,source_name
                        ) DO UPDATE SET value=EXCLUDED.value,unit=EXCLUDED.unit,currency=EXCLUDED.currency,
                          announced_at=EXCLUDED.announced_at,available_at=EXCLUDED.available_at,
                          source_document_id=EXCLUDED.source_document_id,confidence=EXCLUDED.confidence,
                          metadata=EXCLUDED.metadata
                        """,
                        (
                            row["asset_id"],row["stock_code"],metric_code,metric_name,
                            dt.date(int(row["forecast_year"]),12,31),value,unit,row["currency"],
                            row["announced_at"],row["available_at"],source_name,row["document_id"],row["confidence"],
                            Jsonb({
                                "legacy_table": "research.report_forecasts",
                                "report_date": row["report_date"].isoformat() if row["report_date"] else None,
                                "source_unit_requires_document_check": unit == "source_report_unit",
                                "source_metadata": row["metadata"] or {},
                                "semantic_contract": "earnings-forecast-v2",
                            }),
                        ),
                    )
                    forecast_rows += 1

            summary = conn.execute(
                """
                SELECT count(*) AS rows,count(DISTINCT asset_id) AS assets,
                       max(available_at) AS data_through,
                       max(period_end) FILTER (WHERE fact_kind='actual') AS latest_actual_period_end,
                       max(period_end) FILTER (WHERE fact_kind='forecast') AS latest_forecast_period_end,
                       count(*) FILTER (WHERE fact_kind='actual') AS actual,
                       count(*) FILTER (WHERE fact_kind='forecast') AS forecast,
                       count(*) FILTER (WHERE quality_status='verified') AS verified
                FROM company.financial_facts
                """
            ).fetchone()
        quality = "pass" if summary["actual"] and summary["verified"] else (
            "warn" if summary["actual"] else "fail"
        )
        record_quality(
            run["run_id"],"company.financial_facts","typed_fact_kind",
            status="pass",severity="error",observed_value=0,threshold_value=0,
            affected_rows=0,details={"actual_rows": summary["actual"], "forecast_rows": summary["forecast"]},
        )
        set_dataset_watermark(
            "company.financial_facts",schema_version="v3",run_id=run["run_id"],expected_date=until,
            data_through=summary["data_through"],
            row_count=summary["rows"],eligible_count=summary["assets"],coverage_pct=None,
            quality_status=quality,source_version="typed_financial_facts:v1",
            details={
                "actual": summary["actual"],"forecast": summary["forecast"],
                "verified": summary["verified"],
                "latest_actual_period_end": str(summary["latest_actual_period_end"] or ""),
                "latest_forecast_period_end": str(summary["latest_forecast_period_end"] or ""),
                "data_through_semantics": "latest available_at, not forecast period_end",
            },
        )
        run["quality_status"] = quality
        run["output_watermarks"] = {"actual_upserts": actual_rows, "forecast_upserts": forecast_rows}
    print({"actual_upserts": actual_rows,"forecast_upserts": forecast_rows,**dict(summary)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
