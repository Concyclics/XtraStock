"""Real PostgreSQL revision tests, temporary tables and rollback only."""
import sys,uuid,datetime as dt
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from common import connect
from build_adjustment_factors_v3 import BUILD_SQL

def main():
    tables=('market.adjustment_factors_daily','market.corporate_actions','market.canonical_bars_daily_v2')
    sql=BUILD_SQL
    for name in tables:sql=sql.replace(name,'pg_temp.revision_'+name.split('.')[1])
    params={'since':'2000-01-03','until':'2000-01-05','source_complete':True};aid=uuid.uuid4()
    with connect(autocommit=False,attempts=1) as c:
        c.execute("SET LOCAL statement_timeout='10s'");c.execute("SET LOCAL lock_timeout='1s'")
        for name in tables:c.execute('CREATE TEMP TABLE revision_'+name.split('.')[1]+' AS SELECT * FROM '+name+' WITH NO DATA')
        for day in ('2000-01-03','2000-01-04','2000-01-05'):
            c.execute("INSERT INTO revision_canonical_bars_daily_v2(asset_id,dt,close,available_at,quality_status) VALUES(%s,%s,10,'2000-01-05','unverified')",(aid,day))
        for revision,ex,cash in [(1,'2000-01-04',1.),(2,'2000-01-05',.5)]:
            c.execute('''INSERT INTO revision_corporate_actions(action_id,asset_id,action_type,announced_at,ex_date,cash_dividend,
               source_name,source_external_id,revision_no,available_at,quality_status) VALUES(%s,%s,'cash_dividend','2000-01-01',%s,%s,
               'fixture','same-document',%s,clock_timestamp(),'verified')''',(uuid.uuid4(),aid,ex,cash,revision))
        c.execute(sql,params)
        rows=c.execute('SELECT * FROM adjustment_factors_v3_selected ORDER BY dt').fetchall()
        assert [round(r['total_return_factor'],10) for r in rows]==[1.,1.,1.05]
        assert rows[-1]['available_at'].year>=2026
        print('PASS corrected date removes old-date event and keeps actual availability')
        c.execute('DROP TABLE adjustment_factors_v3_selected')
        c.execute('''INSERT INTO revision_corporate_actions(action_id,asset_id,action_type,announced_at,ex_date,cash_dividend,
           source_name,source_external_id,revision_no,available_at,quality_status) VALUES(%s,%s,'cash_dividend','2000-01-02','2000-01-05',.7,
           'fixture','later-publication',1,'2000-01-02','verified')''',(uuid.uuid4(),aid))
        c.execute(sql,params)
        last=c.execute('SELECT * FROM adjustment_factors_v3_selected ORDER BY dt DESC LIMIT 1').fetchone()
        assert abs(last['total_return_factor']-1.07)<1e-10
        print('PASS reparsing older announcement cannot override a newer published plan')
        c.execute('DROP TABLE adjustment_factors_v3_selected')
        c.execute('''INSERT INTO revision_corporate_actions(action_id,asset_id,action_type,announced_at,ex_date,cash_dividend,
           source_name,source_external_id,revision_no,available_at,quality_status) VALUES(%s,%s,'cash_dividend','2000-01-02','2000-01-05',.7,
           'fixture','later-publication',2,clock_timestamp(),'rejected')''',(uuid.uuid4(),aid))
        c.execute(sql,params)
        last=c.execute('SELECT * FROM adjustment_factors_v3_selected ORDER BY dt DESC LIMIT 1').fetchone()
        assert abs(last['total_return_factor']-1.05)<1e-10
        print('PASS latest rejection tombstones earlier accepted document revisions')
        c.rollback()
    print('PASS all fixtures rolled back; no production rows changed')

if __name__=='__main__':main()
