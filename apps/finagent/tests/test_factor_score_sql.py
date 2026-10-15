"""Rollback-only PostgreSQL regression tests for score invalidation and provenance."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect
from build_factor_scores import build_score_sql
from refresh_factor_health import HEALTH_SQL

MAPPING={name:'pg_temp.sc_'+name.replace('.','_') for name in (
 'market.assets','factor.feature_daily','factor.daily_scores','factor.daily_score_revisions',
 'factor.definitions','factor.factor_health_daily')}
def sql(text):
 for source,target in sorted(MAPPING.items(),key=lambda kv:-len(kv[0])):text=text.replace(source,target)
 return text

def main():
 checks=[]
 def ok(name,value):
  if not value:raise AssertionError(name)
  checks.append(name);print('PASS',name,flush=True)
 with connect(autocommit=False,attempts=1) as c:
  c.execute("SET LOCAL statement_timeout='25s'");c.execute("SET LOCAL lock_timeout='2s'")
  for source,target in MAPPING.items():c.execute(f'CREATE TEMP TABLE {target.split(".")[1]} AS SELECT * FROM {source} WITH NO DATA')
  for name,keys in [('factor.daily_scores','factor_id,asset_id,dt'),('factor.factor_health_daily','factor_id,dt')]:
   c.execute(f'CREATE UNIQUE INDEX ON {MAPPING[name]}({keys})')
  migration=(Path(__file__).resolve().parents[1]/'docker/postgres/migrate_factor_score_contract_v1.sql').read_text()
  view_body=migration.split('CREATE OR REPLACE VIEW factor.valid_daily_scores_v1 AS\n',1)[1].split(';\nCOMMENT ON VIEW',1)[0]
  c.execute('CREATE TEMP VIEW sc_valid_scores AS '+sql(view_body))
  def valid_count():return c.execute('SELECT count(*) n FROM sc_valid_scores WHERE dt=%s',(day,)).fetchone()['n']
  day=dt.date(2001,1,2);available=dt.datetime(2001,1,2,8,tzinfo=dt.timezone.utc);cutoff=available+dt.timedelta(hours=5)
  factor=uuid.uuid4();assets=[uuid.uuid4() for _ in range(3)]
  c.execute(sql("INSERT INTO factor.definitions(factor_id,factor_name,lifecycle_status,version) VALUES(%s,'test','shadow',1)"),(factor,))
  for n,a in enumerate(assets,1):
   c.execute(sql("INSERT INTO market.assets(asset_id,asset_type,symbol,metadata) VALUES(%s,'stock',%s,'{}')"),(a,'T'+str(n)))
   c.execute(sql("""INSERT INTO factor.feature_daily(asset_id,dt,momentum_20d,available_at,updated_at,
     input_contract_version,knowledge_mode,input_quality,decision_cutoff)
     VALUES(%s,%s,%s,%s,%s,'pit-input-v1','system','{"strict_pit_ready":true}',%s)"""),(a,day,n,available,available,cutoff))
  params=dict(factor_id=factor,factor_version=1,since=day,until=day,direction=1,min_cohort_size=2,stock_code=None)
  score=sql(build_score_sql('f.momentum_20d'))
  def run():return c.execute(score,params).fetchone()
  def rows():return c.execute(sql('SELECT * FROM factor.daily_scores ORDER BY raw_value NULLS LAST')).fetchall()
  def health():
   c.execute(sql(HEALTH_SQL),dict(since=day,until=day));return c.execute(sql('SELECT * FROM factor.factor_health_daily')).fetchone()
  result=run();r=rows()
  ok('valid three-stock cohort has preserved ranks and provenance',result['changed_rows']==3 and [v['percentile'] for v in r]==[0,.5,1] and all(v['quality_status']=='valid' and v['source_cohort_hash'] for v in r))
  ok('unchanged rerun creates no duplicate history',run()['changed_rows']==0 and c.execute(sql('SELECT count(*) n FROM factor.daily_score_revisions')).fetchone()['n']==0)
  ok('health counts only the matching current scores',health()['scored_assets']==3)
  ok('consumer view accepts the valid positive-control cohort',valid_count()==3)
  c.execute(sql('UPDATE factor.feature_daily SET momentum_20d=NULL,updated_at=updated_at+interval \'1 second\' WHERE asset_id=%s'),(assets[0],))
  ok('one changed cohort member invalidates stale rank coverage before recomputation',health()['scored_assets']==0)
  ok('consumer view rejects stale cross-sectional scores before rebuild',valid_count()==0)
  params['stock_code']='T1';result=run();r=rows()
  dead=next(v for v in r if v['asset_id']==assets[0]);old=c.execute(sql('SELECT previous_row FROM factor.daily_score_revisions')).fetchone()['previous_row']
  ok('NULL replacement clears old value and rank and archives the previous row',result['invalid_rows']==1 and result['archived_rows']==1 and dead['raw_value'] is None and dead['percentile'] is None and dead['zscore'] is None and old['raw_value']==1)
  ok('single-company invalidation does not overwrite other scores',sum(v['quality_status']=='valid' for v in r)==2)
  ok('partial update cannot make remaining stale ranks eligible',health()['scored_assets']==0)
  ok('consumer view rejects old ranks after a single-company rebuild',valid_count()==0)
  params['stock_code']=None;run()
  ok('whole-cohort refresh restores only two usable values',health()['scored_assets']==2)
  ok('consumer view exposes only the refreshed nonmissing inputs',valid_count()==2)
  c.execute(sql('DELETE FROM factor.feature_daily WHERE asset_id=%s'),(assets[1],));run()
  missing=c.execute(sql('SELECT * FROM factor.daily_scores WHERE asset_id=%s'),(assets[1],)).fetchone()
  ok('removed source row invalidates rather than silently preserving its old score',missing['raw_value'] is None and missing['invalid_reason']=='missing_feature')
  remaining=c.execute(sql('SELECT * FROM factor.daily_scores WHERE asset_id=%s'),(assets[2],)).fetchone()
  ok('single observed member retains raw value but gets no percentile',remaining['raw_value']==3 and remaining['percentile'] is None and remaining['zscore'] is None)
  c.execute(sql("UPDATE factor.feature_daily SET input_contract_version='legacy' WHERE asset_id=%s"),(assets[2],));run()
  legacy=c.execute(sql('SELECT * FROM factor.daily_scores WHERE asset_id=%s'),(assets[2],)).fetchone()
  ok('legacy feature cannot retain a valid score',legacy['quality_status']=='invalid' and legacy['invalid_reason']=='legacy_feature_contract' and legacy['raw_value'] is None)
  ok('all-invalid factor explicitly refreshes coverage to zero',health()['scored_assets']==0 and health()['coverage']==0)
  c.execute(sql("UPDATE factor.feature_daily SET input_contract_version='pit-input-v1',knowledge_mode='public' WHERE asset_id=%s"),(assets[2],));run()
  ok('public reconstruction is not relabelled as a strict live score',c.execute(sql('SELECT invalid_reason FROM factor.daily_scores WHERE asset_id=%s'),(assets[2],)).fetchone()['invalid_reason']=='non_strict_feature')
  c.execute(sql("UPDATE factor.feature_daily SET knowledge_mode='system' WHERE asset_id=%s"),(assets[2],));run()
  c.execute(sql('UPDATE factor.definitions SET version=2'))
  ok('changing a factor definition immediately invalidates health until recomputed',health()['scored_assets']==0)
  ok('consumer view rejects outdated factor definition versions',valid_count()==0)
  params['factor_version']=2;run();ok('recomputed score records the new factor definition version',health()['scored_assets']==1)
  c.execute(sql("UPDATE factor.feature_daily SET momentum_20d=3,knowledge_mode='system',updated_at=updated_at+interval '2 seconds'"));run()
  ok('constant cohorts do not receive invented percentiles',all(v['percentile'] is None and v['zscore'] is None for v in rows() if v['quality_status']=='valid'))
  c.execute(sql("UPDATE factor.feature_daily SET momentum_20d='Infinity'::float8 WHERE asset_id=%s"),(assets[2],));run()
  ok('nonfinite expression is invalidated',c.execute(sql('SELECT quality_status FROM factor.daily_scores WHERE asset_id=%s'),(assets[2],)).fetchone()['quality_status']=='invalid')
  c.execute(sql("UPDATE factor.feature_daily SET decision_cutoff=decision_cutoff+interval '1 day' WHERE asset_id=%s"),(assets[0],));run()
  ok('next-day cutoff cannot masquerade as prior-date score availability',c.execute(sql('SELECT invalid_reason FROM factor.daily_scores WHERE asset_id=%s'),(assets[0],)).fetchone()['invalid_reason']=='invalid_availability')
  c.execute(sql("UPDATE market.assets SET metadata='{\"identity_alias_of\":\"fixture\"}' WHERE asset_id=%s"),(assets[0],));run()
  ok('an identity alias loses its old score without deleting its audit history',c.execute(sql('SELECT invalid_reason FROM factor.daily_scores WHERE asset_id=%s'),(assets[0],)).fetchone()['invalid_reason']=='ineligible_asset')
  ok('consumer view excludes nonfinite scores and aliased identities',valid_count()==0)
  c.execute(sql("UPDATE market.assets SET metadata='{\"identity_alias_of\":\"fixture\"}'"))
  empty=health()
  ok('zero identified stocks reports zero coverage rather than PostgreSQL least-NULL 100 percent',empty['eligible_assets']==0 and empty['coverage']==0)
  c.rollback()
 print(json.dumps(dict(passed=len(checks),checks=checks,production_writes=0),ensure_ascii=False))
 return 0
if __name__=='__main__':raise SystemExit(main())
