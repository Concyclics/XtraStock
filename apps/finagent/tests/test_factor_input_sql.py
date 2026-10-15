"""Real PostgreSQL contract tests in temporary tables; always roll back.
Run explicitly: .venv/bin/python tests/test_factor_input_sql.py
No production rows are written and no full corpus is scanned.
"""
from __future__ import annotations
import datetime as dt
import json
import math
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect
from build_technical_signals import build_sql
from build_factor_features import FEATURE_SQL,LABEL_SQL
from factor_dsl import compile_expression
from evaluate_factors import evaluate

MAPPING={name:'pg_temp.fc_'+name.replace('.','_') for name in (
    'market.assets','market.trading_calendar','market.canonical_total_return_daily_v2',
    'market.adjustment_factors_daily','market.canonical_bars_daily_v2','analytics.asset_technical_daily',
    'factor.feature_daily','factor.forward_returns','market.money_flow_daily','research.reports',
    'corpus.documents','analytics.document_events','analytics.graph_feature_daily','market.asset_lifecycle_versions')}

def rewritten(sql):
    # Both raw and identity-resolved report fixtures share the same temporary
    # rows; alias knowledge time is tested independently of source publication.
    sql=sql.replace('research.reports_resolved','research.reports')
    for source,target in sorted(MAPPING.items(),key=lambda kv:-len(kv[0])):sql=sql.replace(source,target)
    return sql

class Cursor:
    def __init__(self,c):self.c=c
    def execute(self,sql,params=None):return self.c.execute(rewritten(sql),params)

def main():
    checks=[]
    def ok(name,test):
        if not test:raise AssertionError(name)
        checks.append(name);print('PASS',name,flush=True)
    with connect(autocommit=False,attempts=1) as c:
        c.execute("SET LOCAL statement_timeout='25s'")
        c.execute("SET LOCAL lock_timeout='2s'")
        for source,target in MAPPING.items():c.execute(f'CREATE TEMP TABLE {target.split(".")[1]} AS SELECT * FROM {source} WITH NO DATA')
        c.execute(f"ALTER TABLE {MAPPING['research.reports']} ADD COLUMN identity_known_at timestamptz")
        for tab,keys in (('analytics.asset_technical_daily','asset_id,dt'),('factor.feature_daily','asset_id,dt'),('factor.forward_returns','asset_id,signal_date')):
            c.execute(f'CREATE UNIQUE INDEX ON {MAPPING[tab]}({keys})')
        a=uuid.uuid4();days=[];day=dt.date(2001,1,1)
        while len(days)<90:
            if day.weekday()<5:days.append(day)
            day+=dt.timedelta(days=1)
        for i,day in enumerate(days):
            c.execute(f"INSERT INTO {MAPPING['market.trading_calendar']}(exchange,trade_date,session_status,is_verified,source_name) VALUES('SSE',%s,'open',true,'fixture')",(day,))
        c.execute(f"INSERT INTO {MAPPING['market.assets']}(asset_id,asset_type,symbol,name,metadata) VALUES(%s,'stock','TEST01','Fixture',%s)",(a,json.dumps({})))
        for i,day in enumerate(days):
            av=dt.datetime.combine(day,dt.time(7,5),dt.timezone.utc)
            c.execute(f"""INSERT INTO {MAPPING['market.canonical_total_return_daily_v2']}
              (asset_id,dt,open,high,low,close,volume,amount,source,adjustment,available_at,factor_available_at,quality_status)
              VALUES(%s,%s,%s,%s,%s,%s,10000,1000000,'fixture','total_return',%s,%s,'unverified')""",
              (a,day,100+i,102+i,99+i,101+i,av,av))
            c.execute(f"INSERT INTO {MAPPING['market.adjustment_factors_daily']}(asset_id,dt,quality_status,total_return_factor) VALUES(%s,%s,'verified',1)",(a,day))
        asof=days[80];params={'since':asof,'until':asof,'asset_types':['stock'],'stock_code':None}
        c.execute(rewritten(build_sql(None)),params)
        row=c.execute(f"SELECT * FROM {MAPPING['analytics.asset_technical_daily']} WHERE asset_id=%s AND dt=%s",(a,asof)).fetchone()
        ok('complete 20-session feature has the correct horizon',abs(row['ret_20d']-(181/161-1))<1e-12 and row['input_quality']['status']=='pass')
        removed=c.execute(f"DELETE FROM {MAPPING['market.canonical_total_return_daily_v2']} WHERE asset_id=%s AND dt=%s RETURNING *",(a,days[70])).fetchone()
        c.execute(rewritten(build_sql(None)),params)
        row=c.execute(f"SELECT * FROM {MAPPING['analytics.asset_technical_daily']} WHERE asset_id=%s AND dt=%s",(a,asof)).fetchone()
        ok('one missing exchange session invalidates the 20-session window',row['ret_20d'] is None and row['volatility_20d'] is None)
        cols=list(removed);c.execute(f"INSERT INTO {MAPPING['market.canonical_total_return_daily_v2']} ({','.join(cols)}) VALUES ({','.join(['%s']*len(cols))})",tuple(json.dumps(v) if isinstance(v,dict) else v for v in removed.values()))
        c.execute(f"UPDATE {MAPPING['market.adjustment_factors_daily']} SET quality_status='unverified' WHERE asset_id=%s AND dt=%s",(a,days[75]))
        c.execute(rewritten(build_sql(None)),params)
        row=c.execute(f"SELECT ret_20d FROM {MAPPING['analytics.asset_technical_daily']} WHERE asset_id=%s AND dt=%s",(a,asof)).fetchone()
        ok('unverified corporate action invalidates an otherwise complete return window',row['ret_20d'] is None)
        c.execute(f"UPDATE {MAPPING['market.adjustment_factors_daily']} SET quality_status='verified' WHERE asset_id=%s",(a,))
        c.execute(rewritten(build_sql(None)),params)
        label_params={'since':days[0],'until':days[10]}
        c.execute(rewritten(LABEL_SQL),label_params)
        label=c.execute(f"SELECT * FROM {MAPPING['factor.forward_returns']} WHERE asset_id=%s AND signal_date=%s",(a,days[0])).fetchone()
        ok('A-share same-day exit is excluded but the intraday diagnostic is retained',label['return_1d'] is None and label['intraday_return_next_session'] is not None)
        ok('5/20/60-day labels mature separately and generic availability is conservative',label['label_available_at_5d']<label['label_available_at_20d']<label['label_available_at_60d']==label['label_available_at'])
        c.execute(f"DELETE FROM {MAPPING['market.canonical_total_return_daily_v2']} WHERE asset_id=%s AND dt=%s",(a,days[5]))
        c.execute(rewritten(LABEL_SQL),label_params)
        label=c.execute(f"SELECT * FROM {MAPPING['factor.forward_returns']} WHERE asset_id=%s AND signal_date=%s",(a,days[0])).fetchone()
        ok('a missing future session cannot silently lengthen the holding label',label['return_5d'] is None and label['label_available_at_5d'] is None)
        c.execute("SELECT set_config('finagent.knowledge_mode','system',true),set_config('finagent.decision_time','21:00:00',true)")
        graph_av=dt.datetime.combine(asof,dt.time(12),dt.timezone.utc) # Beijing 20:00
        c.execute(f"INSERT INTO {MAPPING['analytics.graph_feature_daily']}(asset_id,dt,direct_exposure_score,available_at,provenance) VALUES(%s,%s,3,%s,%s)",(a,asof,graph_av,json.dumps({'knowledge_mode':'system','input_contract_version':'graph-pit-v1','strict_pit_ready':True})))
        c.execute(rewritten(FEATURE_SQL),{'since':asof,'until':asof})
        feature=c.execute(f"SELECT * FROM {MAPPING['factor.feature_daily']} WHERE asset_id=%s AND dt=%s",(a,asof)).fetchone()
        ok('the composed feature keeps graph availability instead of backdating to close',feature['available_at']==graph_av and feature['graph_direct_exposure']==3 and feature['input_quality']['strict_pit_ready'])
        c.execute(f"UPDATE {MAPPING['analytics.graph_feature_daily']} SET available_at=%s WHERE asset_id=%s",(graph_av+dt.timedelta(hours=2),a))
        c.execute(rewritten(FEATURE_SQL),{'since':asof,'until':asof})
        feature=c.execute(f"SELECT * FROM {MAPPING['factor.feature_daily']} WHERE asset_id=%s AND dt=%s",(a,asof)).fetchone()
        ok('post-cutoff graph evidence is excluded',feature['graph_direct_exposure'] is None and feature['available_at']<=feature['decision_cutoff'])
        expr=compile_expression({'op':'clip','arg':{'op':'field','field':'momentum_20d'},'lower':-1,'upper':1})
        result=c.execute(f"SELECT {expr} result FROM (SELECT NULL::double precision momentum_20d) f").fetchone()
        ok('clip preserves SQL NULL instead of inventing a lower-bound observation',result['result'] is None)
        # Positive control before changing only the lifecycle: labels and fills are present.
        c.execute(f"INSERT INTO {MAPPING['market.canonical_bars_daily_v2']}(asset_id,dt,volume,high,low,quality_status) SELECT asset_id,dt,volume,high,low,quality_status FROM {MAPPING['market.canonical_total_return_daily_v2']}")
        c.execute(rewritten(LABEL_SQL),{'since':asof,'until':asof})
        c.execute(f"INSERT INTO {MAPPING['market.asset_lifecycle_versions']}(lifecycle_version_id,asset_id,valid_from,lifecycle_status,is_tradable,is_st,is_suspended,available_at,quality_status) VALUES(1,%s,%s,'active',true,false,false,%s,'verified')",(a,asof,graph_av-dt.timedelta(days=2)))
        result=evaluate(Cursor(c),'f.momentum_20d',1,asof,asof,5)
        ok('positive-control evaluation accepts a visible tradable lifecycle',result['observation_count']==1)
        c.execute(f"UPDATE {MAPPING['market.asset_lifecycle_versions']} SET lifecycle_status='unknown' WHERE asset_id=%s",(a,))
        result=evaluate(Cursor(c),'f.momentum_20d',1,asof,asof,5)
        ok('changing only lifecycle to unknown removes the observation',result['observation_count']==0)
        c.execute(f"UPDATE {MAPPING['market.asset_lifecycle_versions']} SET lifecycle_status='active',available_at=%s WHERE asset_id=%s",(graph_av+dt.timedelta(days=1),a))
        result=evaluate(Cursor(c),'f.momentum_20d',1,asof,asof,5)
        ok('future-recorded lifecycle cannot leak into the historical universe',result['observation_count']==0)
        c.rollback()
    print(json.dumps({'passed':len(checks),'checks':checks,'production_writes':0},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
