"""Compare pre-pruning and bounded score SQL on tiny pg_temp data, then roll back."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import sys
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect,jsonb
from build_factor_scores import build_score_sql

TABLES=('market.assets','factor.feature_daily','factor.daily_scores','factor.daily_score_revisions')

def mapped(text,prefix):
    for name in sorted(TABLES,key=len,reverse=True):text=text.replace(name,'pg_temp.'+prefix+'_'+name.replace('.','_'))
    return text


def main():
    checks=[]
    def ok(name,value):
        if not value:raise AssertionError(name)
        checks.append(name);print('PASS',name,flush=True)
    old_sql=(Path(__file__).resolve().parent/'fixtures/factor_score_pre_pruning.sql').read_text()
    new_sql=build_score_sql('f.momentum_20d')
    day=dt.date(2001,1,3);before=day-dt.timedelta(days=1);after=day+dt.timedelta(days=1)
    factor=uuid.uuid4();other=uuid.uuid4();assets=[uuid.uuid4() for _ in range(4)]
    available=dt.datetime(2001,1,3,8,tzinfo=dt.timezone.utc)
    with connect(autocommit=False,attempts=1) as c:
        c.execute("SET LOCAL statement_timeout='15s'");c.execute("SET LOCAL lock_timeout='1ms'")
        def execute(prefix,sql,params=None):return c.execute(mapped(sql,prefix),params)
        for prefix in ('old','new'):
            for source in TABLES:
                c.execute(f'CREATE TEMP TABLE {prefix}_{source.replace(".","_")} AS SELECT * FROM {source} WITH NO DATA')
            c.execute(f'CREATE UNIQUE INDEX ON {prefix}_factor_daily_scores(factor_id,asset_id,dt)')
            for n,asset in enumerate(assets,1):
                execute(prefix,"INSERT INTO market.assets(asset_id,asset_type,symbol,metadata) VALUES(%s,'stock',%s,'{}')",(asset,'T'+str(n)))
            for d in (before,day,after):
                at=available+dt.timedelta(days=(d-day).days)
                for n,asset in enumerate(assets[:3],1):
                    execute(prefix,"""INSERT INTO factor.feature_daily
                        (asset_id,dt,momentum_20d,available_at,updated_at,input_contract_version,knowledge_mode,input_quality,decision_cutoff)
                        VALUES(%s,%s,%s,%s,%s,'pit-input-v1','system','{"strict_pit_ready":true}',%s)""",
                        (asset,d,n,at,at,at+dt.timedelta(hours=5)))
                for asset in assets:
                    if d==day and asset==assets[1]:continue  # a genuinely new score, no old-side row
                    execute(prefix,"""INSERT INTO factor.daily_scores
                        (factor_id,asset_id,dt,raw_value,percentile,zscore,available_at,source_feature_date,input_contract_version,quality_status,source_factor_version,updated_at)
                        VALUES(%s,%s,%s,90,.9,1,%s,%s,'legacy','legacy',0,%s)""",(factor,asset,d,at,d,at))
            for asset in assets:
                execute(prefix,"""INSERT INTO factor.daily_scores
                    (factor_id,asset_id,dt,raw_value,percentile,zscore,available_at,source_feature_date,input_contract_version,quality_status,source_factor_version,updated_at)
                    VALUES(%s,%s,%s,777,.7,1,%s,%s,'legacy','legacy',0,%s)""",(other,asset,day,available,day,available))
            execute(prefix,"""INSERT INTO factor.daily_score_revisions(factor_id,asset_id,dt,previous_row,replacement_reason)
                VALUES(%s,%s,%s,%s,'preexisting_other_factor_history')""",(other,assets[0],before,jsonb({'sentinel':'unchanged'})))
        params=dict(factor_id=factor,factor_version=1,since=day,until=day,direction=1,min_cohort_size=2,stock_code=None)
        def snapshot(prefix):
            scores=execute(prefix,'SELECT to_jsonb(s) AS row FROM factor.daily_scores s ORDER BY factor_id,asset_id,dt').fetchall()
            history=execute(prefix,"""SELECT factor_id,asset_id,dt,previous_row,replacement_reason
                FROM factor.daily_score_revisions ORDER BY factor_id,asset_id,dt,previous_row::text,replacement_reason""").fetchall()
            return scores,history
        def sentinels(prefix):
            return execute(prefix,"SELECT to_jsonb(s) AS row FROM factor.daily_scores s WHERE dt<>%s OR factor_id<>%s ORDER BY factor_id,asset_id,dt",(day,factor)).fetchall()
        initial=sentinels('old')
        def compare(label):
            a=execute('old',old_sql,params).fetchone();b=execute('new',new_sql,params).fetchone()
            ok(label+' output and complete audit history equal',a==b and snapshot('old')==snapshot('new'))
            ok(label+' leaves other dates and factors byte-equivalent',sentinels('old')==initial and sentinels('new')==initial)
            return b
        r=compare('initial legacy replacement plus new/missing source rows')
        ok('missing source stays target of invalidation and archive',r['changed_rows']==4 and r['invalid_rows']==1 and r['archived_rows']==3)
        missing=execute('new','SELECT raw_value,invalid_reason FROM factor.daily_scores WHERE factor_id=%s AND asset_id=%s AND dt=%s',(factor,assets[3],day)).fetchone()
        ok('LEFT JOIN missing feature is not removed by the date bound',missing=={'raw_value':None,'invalid_reason':'missing_feature'})
        ok('identical rerun remains a no-op',compare('idempotent rerun')['changed_rows']==0)
        for prefix in ('old','new'):
            execute(prefix,"UPDATE factor.feature_daily SET momentum_20d=NULL,updated_at=updated_at+interval '1 second' WHERE dt=%s AND asset_id=%s",(day,assets[0]))
        params['stock_code']='T1';compare('single-stock NULL invalidation')
        params['stock_code']='T2';compare('single-stock ranked against full date cohort')
        rank=execute('new','SELECT percentile FROM factor.daily_scores WHERE factor_id=%s AND asset_id=%s AND dt=%s',(factor,assets[1],day)).fetchone()['percentile']
        ok('single-stock write uses two-stock full cross-section rank',rank==0)
        params['stock_code']=None;compare('whole-date cohort reconciliation')
        for prefix in ('old','new'):
            execute(prefix,'DELETE FROM factor.feature_daily WHERE dt=%s AND asset_id=%s',(day,assets[1]))
        compare('removed feature invalidates preserved legacy score')
        for prefix in ('old','new'):
            execute(prefix,"UPDATE factor.feature_daily SET input_contract_version='legacy' WHERE dt=%s AND asset_id=%s",(day,assets[2]))
        compare('old input contract invalidation')
        params['factor_version']=2;compare('new factor version transition')
        ok('other-factor audit sentinel preserved alone',execute('new','SELECT count(*) n FROM factor.daily_score_revisions WHERE factor_id=%s',(other,)).fetchone()['n']==1)
        c.rollback()
    print(json.dumps({'passed':len(checks),'production_writes':0,'checks':checks},ensure_ascii=False))
    return 0

if __name__=='__main__':raise SystemExit(main())
