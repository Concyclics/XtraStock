"""Versioned research/data readiness, separate from job exit status or model approval."""
from __future__ import annotations
import datetime as dt
import json
from zoneinfo import ZoneInfo
try:
    from .lifecycle_evidence import lifecycle_order
except ImportError:
    from lifecycle_evidence import lifecycle_order

VERSION='quant-readiness-v1'

def assess(e):
    c=[]
    def add(name,ok,observed,required,reason):
        c.append({'name':name,'passed':bool(ok),'observed':observed,'required':required,'reason':reason})
    days=e.get('calendar',{});features=e.get('features',{});life=e.get('lifecycle',{})
    add('historical_sessions',days.get('sessions',0)>=60,days.get('sessions',0),60,'至少60个真实开市日的量价研究窗口')
    add('daily_market_coverage',days.get('low_coverage_days',999)==0,days.get('low_coverage_days'),0,'历史整市场缺日须补齐；这是全市场粗门禁，个股另查窗口')
    add('strict_feature_contract',features.get('strict',0)>=30,features.get('strict',0),30,'至少30资产具有当前系统时点、窗口和复权契约')
    add('verified_lifecycle',life.get('verified_active',0)>=30,life.get('verified_active',0),30,'至少30只具备已核实当期生命周期的可交易证券')
    add('legacy_graph_review',e.get('invalid_legacy',-1)==0,e.get('invalid_legacy'),0,'已拒绝或候选旧关系不得继续正式计分')
    add('benchmark_history',e.get('benchmark_days',0)>=60,e.get('benchmark_days',0),60,'同口径基准至少60个交易日，价格指数不冒充全收益指数')
    add('frozen_snapshot',e.get('snapshot_current',False),e.get('snapshot_current'),True,'使用本数据日实际已完成的冻结快照')
    return {'contract_version':VERSION,'status':'ready' if all(x['passed'] for x in c) else 'blocked','checks':c}

def collect(conn,as_of):
    cutoff=dt.datetime.now(dt.timezone.utc)
    p={'date':as_of,'cutoff':cutoff}
    e={}
    e['calendar']=dict(conn.execute("""WITH sessions AS(
      SELECT trade_date FROM market.trading_calendar WHERE exchange='SSE' AND session_status='open'
      AND is_verified AND trade_date<=%(date)s ORDER BY trade_date DESC LIMIT 60
    ), n AS(SELECT s.trade_date,count(a.asset_id) n FROM sessions s
      LEFT JOIN market.canonical_bars_daily_v2 b ON b.dt=s.trade_date
      LEFT JOIN market.assets a ON a.asset_id=b.asset_id AND a.asset_type='stock'
      GROUP BY s.trade_date), threshold AS(SELECT greatest(3000,max(n)*.95) floor FROM n)
      SELECT count(*) sessions,min(n) minimum_assets,count(*) FILTER(WHERE n<floor) low_coverage_days
      FROM n CROSS JOIN threshold""",p).fetchone())
    e['features']=dict(conn.execute("""SELECT count(*) total,count(*) FILTER(WHERE
      input_contract_version='pit-input-v1' AND knowledge_mode='system'
      AND input_quality->>'strict_pit_ready'='true' AND available_at<=%(cutoff)s) strict
      FROM factor.feature_daily WHERE dt=%(date)s""",p).fetchone())
    e['lifecycle']=dict(conn.execute(f"""WITH latest AS(SELECT DISTINCT ON(asset_id) *
      FROM market.asset_lifecycle_versions WHERE valid_from<=%(date)s AND (valid_to IS NULL OR valid_to>=%(date)s)
      AND available_at<=%(cutoff)s ORDER BY asset_id,{lifecycle_order()})
      SELECT count(*) FILTER(WHERE quality_status='verified' AND lifecycle_status='active' AND is_tradable AND NOT is_st) verified_active FROM latest""",p).fetchone())
    e['invalid_legacy']=conn.execute("""SELECT count(*) n FROM kg.relation_versions v JOIN kg.relations r USING(relation_id)
      JOIN kg.company_concept_exposures x ON x.exposure_id=split_part(r.relation_key,':',3)::uuid
      WHERE v.source_system='company_concept_exposures' AND v.status='active'
      AND r.relation_key LIKE 'relation:exposure:%%' AND x.status<>'active'
      AND v.valid_during @> %s::date AND v.system_known_during @> %s::timestamptz""",(as_of,cutoff)).fetchone()['n']
    if conn.execute("SELECT to_regclass('market.benchmark_daily_v1') t").fetchone()['t']:
        e['benchmark_days']=conn.execute("""SELECT count(DISTINCT b.dt) n FROM market.benchmark_daily_v1 b
          JOIN market.assets a USING(asset_id) WHERE a.exchange='SSE' AND a.symbol='000300'
          AND a.asset_type='index' AND b.quality_status='verified' AND b.dt<=%s
          AND b.dt>=%s::date-130""",(as_of,as_of)).fetchone()['n']
    else:e['benchmark_days']=0
    row=conn.execute("""SELECT snapshot_id,as_of_date,decision_cutoff,completed_at FROM analytics.research_snapshot_runs
       WHERE status='complete' AND version='research-snapshot-v5' AND as_of_date=%s AND completed_at<=%s AND decision_cutoff<=%s
       ORDER BY completed_at DESC LIMIT 1""",(as_of,cutoff,cutoff)).fetchone()
    current=bool(row)
    if row:
        current=not conn.execute("""SELECT EXISTS(SELECT 1 FROM factor.feature_daily WHERE dt=%s AND updated_at>%s)
          OR EXISTS(SELECT 1 FROM factor.daily_scores WHERE dt=%s AND quality_status='valid' AND updated_at>%s) changed""",
          (as_of,row['completed_at'],as_of,row['completed_at'])).fetchone()['changed']
    e['snapshot_current']=current;e['snapshot']=dict(row) if row else None
    e['checked_at']=cutoff.isoformat();return e

def save(conn,as_of,e):
    from psycopg.types.json import Jsonb
    result=assess(e)
    r=conn.execute("""INSERT INTO quant.readiness_runs(as_of_date,contract_version,status,checks,evidence)
      VALUES(%s,%s,%s,%s,%s) RETURNING readiness_id""",(as_of,VERSION,result['status'],Jsonb(result['checks']),
      Jsonb(json.loads(json.dumps(e,default=str))))).fetchone()
    return {**result,'readiness_id':str(r['readiness_id'])}
