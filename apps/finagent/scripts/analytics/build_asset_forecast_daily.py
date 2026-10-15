#!/usr/bin/env python3
"""Typed EPS (CNY/share) and parent net-profit (CNY) aggregates.

Consensus gives each institution one latest vote per fiscal year. Growth uses
adjacent years from the same dated report. Unverified legacy years never enter.
"""
from __future__ import annotations
import argparse
from common import connect


def build_sql():
    metric_cols=[];metric_expr=[];consensus=[]
    for m in ('eps','net_profit'):
        metric_cols += [f'forecast_{m}',f'next_forecast_{m}',f'later_forecast_{m}',f'forecast_{m}_growth',
                       f'consensus_{m}',f'consensus_{m}_median',f'consensus_{m}_stddev']
        metric_expr += [f'fc.{m}',f'fn.{m}',f'fl.{m}',
             f'CASE WHEN fc.{m}>0 THEN fn.{m}/fc.{m}-1 END',f'c.avg_{m}',f'c.median_{m}',f'c.stddev_{m}']
        consensus += [f'avg({m}) AS avg_{m}',f'percentile_cont(.5) WITHIN GROUP(ORDER BY {m}) AS median_{m}',f'stddev_samp({m}) AS stddev_{m}']
    cols=['asset_id','stock_code','stock_name','as_of_date','latest_report_date','forecast_report_date','forecast_document_id','forecast_year','next_forecast_year','later_forecast_year']+metric_cols+[
        'forward_pe','next_forward_pe','latest_rating','latest_rating_change','forecast_report_count_180d','forecast_institution_count_180d','consensus_pe',
        'actual_annual_report_date','actual_annual_net_profit','forecast_vs_actual_growth','source_document_ids','updated_at']
    expr=['lr.asset_id','lr.stock_code','lr.stock_name','p.as_of','lr.report_date','fc.report_date','fc.document_id','fc.forecast_year','fn.forecast_year','fl.forecast_year']+metric_expr+[
        'fc.pe','fn.pe','lr.rating','lr.rating_change','coalesce(c.n,0)','coalesce(c.institutions,0)','c.avg_pe','aa.period_end','aa.value',
        "CASE WHEN aa.value>0 AND fc.forecast_year=extract(year FROM aa.period_end)+1 THEN fc.net_profit/aa.value-1 END",'coalesce(c.docs,ARRAY[]::uuid[])','now()']
    return """
WITH params AS (
 SELECT coalesce(%(as_of)s::date,(SELECT max(report_date) FROM research.reports_resolved),current_date) AS as_of,
 %(lookback_days)s::int AS lookback
), reports AS (
 SELECT r.*,a.asset_id,coalesce(nullif(r.stock_name,''),a.name) AS resolved_name
 FROM research.reports_resolved r LEFT JOIN market.assets a ON a.asset_type='stock' AND a.symbol=r.stock_code
 CROSS JOIN params p WHERE r.report_type='stock' AND r.report_date<=p.as_of
 AND greatest(coalesce(r.available_at,r.created_at),r.identity_known_at)<((p.as_of+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
), base AS (
 SELECT r.document_id,r.stock_code,r.institution,r.report_date,f.forecast_year,f.eps,f.net_profit,f.pe
 FROM reports r JOIN research.report_forecasts_usable f USING(document_id) CROSS JOIN params p
 WHERE r.report_date>=p.as_of-p.lookback
 AND greatest(f.created_at,f.updated_at)<((p.as_of+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
), lr AS (
 SELECT DISTINCT ON(stock_code) asset_id,stock_code,resolved_name AS stock_name,report_date,rating,rating_change
 FROM reports ORDER BY stock_code,report_date DESC,document_id
), fc AS (
 SELECT DISTINCT ON(stock_code) b.* FROM base b CROSS JOIN params p
 WHERE b.forecast_year=extract(year FROM p.as_of)::int
 ORDER BY stock_code,report_date DESC,document_id
), latest_institution AS (
 SELECT DISTINCT ON(stock_code,institution,forecast_year) b.* FROM base b CROSS JOIN params p
 WHERE forecast_year=extract(year FROM p.as_of)::int AND nullif(institution,'') IS NOT NULL
 ORDER BY stock_code,institution,forecast_year,report_date DESC,document_id
), consensus AS (
 SELECT stock_code,count(*)::int n,count(DISTINCT institution)::int institutions,
 array_agg(DISTINCT document_id) docs,avg(pe) FILTER(WHERE pe>0) AS avg_pe,
 """+','.join(consensus)+""" FROM latest_institution GROUP BY stock_code
), actual_annual AS (
 SELECT DISTINCT ON(stock_code) stock_code,period_end,value
 FROM company.financial_facts CROSS JOIN params p
 WHERE fact_kind='actual' AND metric_code='net_profit_parent' AND statement_scope='consolidated'
 AND quality_status='verified' AND unit='CNY' AND currency='CNY' AND period_type='annual'
 AND period_end=make_date(extract(year FROM p.as_of)::int-1,12,31)
 AND available_at<((p.as_of+1)::timestamp AT TIME ZONE 'Asia/Shanghai')
 ORDER BY stock_code,period_end DESC,available_at DESC,financial_fact_id
)
INSERT INTO analytics.asset_forecast_daily ("""+','.join(cols)+") SELECT "+','.join(expr)+"""
FROM lr CROSS JOIN params p LEFT JOIN fc USING(stock_code)
LEFT JOIN base fn ON fn.document_id=fc.document_id AND fn.forecast_year=fc.forecast_year+1
LEFT JOIN base fl ON fl.document_id=fc.document_id AND fl.forecast_year=fc.forecast_year+2
LEFT JOIN consensus c ON c.stock_code=lr.stock_code LEFT JOIN actual_annual aa ON aa.stock_code=lr.stock_code
ON CONFLICT(stock_code,as_of_date) DO UPDATE SET """+','.join(f'{k}=EXCLUDED.{k}' for k in cols if k not in ('stock_code','as_of_date'))


def main():
    p=argparse.ArgumentParser();p.add_argument('--as-of');p.add_argument('--lookback-days',type=int,default=180);a=p.parse_args()
    with connect(autocommit=False) as c:
        c.execute("SET LOCAL lock_timeout='5s'");c.execute("SET LOCAL statement_timeout='180s'");c.execute('SET LOCAL jit=off')
        result=c.execute(build_sql(),{'as_of':a.as_of,'lookback_days':a.lookback_days});print(f'forecast_rows={result.rowcount}')
    return 0
if __name__=='__main__':raise SystemExit(main())
