import copy
import datetime as dt
import json
from unittest.mock import patch
import numpy as np
import unittest
from scripts.analytics.candidate_pool import config,score_candidate,evolve,TZ
from scripts.analytics.research_history import history_plan
from scripts.analytics.reconstructed_factor_research import evaluate_rows

DAY=dt.date(2026,10,14)
NOW=dt.datetime(2026,10,14,20,tzinfo=TZ)

def source(code='000001',growth=20,quality='verified'):
    return {'stock_code':code,'stock_name':'fixture','industry_name':'industry-'+code,
        'market':{'quality_status':'unverified','close':10,'amount':30_000_000,'dt':str(DAY)},
        'actual_financials':[{'metric_code':metric,'period_end':'2026-06-30','unit':'CNY','yoy':growth,
             'quality_status':quality,'value':100} for metric in ('revenue','net_profit_parent')],
        'factors':[{'factor_name':'momentum_20d','percentile':.6,'dt':str(DAY)}],
        'filings':[],'graph':[]}

def cfg():return {**config(),'exploration_capacity':8,'watch_capacity':4,'core_capacity':2,'weekly_replacements':1,'industry_core_limit':1}
def scored(code,growth):return score_candidate(source(code,growth),DAY)

def test_administrative_filing_volume_cannot_become_bullish_score():
    p=source();a=score_candidate(p,DAY)
    p['filings']=[{'title':'董事会会议公告','published_at':str(DAY)}]*100
    b=score_candidate(p,DAY)
    assert a['score']==b['score'] and a['long_term_score']==b['long_term_score']
    assert '新公告待分析' in b['channels']

def test_unverified_stale_and_future_financials_do_not_qualify_core():
    for quality,date in [('unverified','2026-06-30'),('verified','2020-06-30'),('verified','2027-06-30')]:
        p=source(quality=quality)
        for f in p['actual_financials']:f['period_end']=date
        row=score_candidate(p,DAY)
        assert row['long_term_score'] is None and not row['core_eligible']

def test_classified_graph_is_discovery_not_directional_proof():
    p=source();base=score_candidate(p,DAY)
    p['graph']=[{'review_status':'classified','direction':1,'confidence':1}]
    assert score_candidate(p,DAY)['score']==base['score']
    p['graph'][0]['review_status']='approved'
    assert score_candidate(p,DAY)['score']==base['score']+5

def test_feedback_penalty_does_not_rewrite_long_term_thesis():
    p=source();base=score_candidate(p,DAY)
    review=score_candidate(p,DAY,{'review_required':True,'mature_observations':25,'availability':'available'})
    assert review['score']==base['score']-5 and review['long_term_score']==base['long_term_score']
    unknown=score_candidate(p,DAY,{'availability':'unavailable'})
    assert unknown['score']==base['score'] and unknown['feedback_status']=='反馈暂不可读取'

def test_missing_timing_cannot_improve_rank_and_loss_cannot_enter_core():
    p=source();full=score_candidate(p,DAY)
    p['factors']=[];missing=score_candidate(p,DAY)
    assert missing['score']<full['score'] and not missing['core_eligible']
    p=source();p['actual_financials'][1]['value']=-10
    loss=score_candidate(p,DAY)
    assert not loss['core_eligible'] and loss['financial_risks']

def test_unknown_industries_share_concentration_budget():
    rows=[scored(f'{i:06d}',30) for i in range(10)]
    for r in rows:r['industry']=None
    out,s=evolve(rows,[],DAY,NOW,cfg())
    assert s['state_counts']['core']==1

def test_core_industry_limit_and_maximum_capacities():
    rows=[scored(f'{i:06d}',50-i) for i in range(20)]
    for r in rows:r['industry']='same'
    out,s=evolve(rows,[],DAY,NOW,cfg())
    assert s['state_counts']['core']==1
    assert s['state_counts']['core']+s['state_counts']['watching']==4
    assert len(out)==8

def test_daily_discovery_does_not_churn_weekly_membership():
    rows=[scored(f'{i:06d}',i*4) for i in range(6)]
    old,s=evolve(rows,[],DAY,NOW,cfg())
    out,s2=evolve(rows+[scored('999999',100)],old,DAY,NOW+dt.timedelta(days=1),cfg(),s)
    assert {r['stock_code'] for r in out if r['state']=='core'}=={r['stock_code'] for r in old if r['state']=='core'}
    assert any(r['stock_code']=='999999' and r['state']=='exploring' for r in out)
    assert not s2['weekly_rebalanced']

def test_weekly_replacement_limit_and_holding_period():
    rows=[scored(f'{i:06d}',i) for i in range(4)]
    old,s=evolve(rows,[],DAY,NOW,cfg())
    new=rows+[scored('888888',90),scored('999999',100)]
    out,s2=evolve(new,old,DAY,NOW+dt.timedelta(days=8),cfg(),s)
    assert s2['core_replacements']==1
    assert len([r for r in out if r['state']=='core'])==2
    before_watch={r['stock_code'] for r in old if r['state'] in ('core','watching')}
    after_watch={r['stock_code'] for r in out if r['state'] in ('core','watching')}
    assert len(after_watch-before_watch)<=cfg()['weekly_replacements']
    held,s3=evolve(new,old,DAY,NOW+dt.timedelta(days=5),cfg(),s)
    assert s3['core_replacements']==0

def test_monthly_thesis_keeps_previous_baseline_until_review():
    old,s=evolve([scored('000001',5)],[],DAY,NOW,cfg())
    changed=scored('000001',45)
    daily,_=evolve([changed],old,DAY,NOW+dt.timedelta(days=1),cfg(),s)
    assert daily[0]['monthly_review']==old[0]['monthly_review']
    monthly,summary=evolve([changed],old,DAY,dt.datetime(2026,11,2,8,tzinfo=TZ),cfg(),s)
    assert summary['monthly_reviewed'] and monthly[0]['monthly_review']['change']=='披露指标或研究论点更新'
    assert monthly[0]['monthly_review']['next_review_date']=='2026-12-01'

def test_known_invalid_market_leaves_active_pool_without_deleting_history():
    row=scored('000001',30);old,s=evolve([row],[],DAY,NOW,cfg())
    row.update(eligible=False,core_eligible=False)
    out,_=evolve([row],old,DAY,NOW+dt.timedelta(days=1),cfg(),s)
    assert out[0]['state']=='cooling' and out[0]['previous_state']=='core'

def test_configurable_prefix_formula_and_horizon_floor():
    assert history_plan()['required_eligible_sessions']==800
    assert history_plan(126,40,2,20)['required_eligible_sessions']==246
    assert history_plan(126,40,2,5)['required_eligible_sessions']==216
    assert history_plan(60,20,1,60)['test_sessions']==120
    for args in [(0,40,2,20),(126,1,2,20),(126,40,0,20),(126,40,2,1)]:
        with unittest.TestCase().assertRaises(ValueError):history_plan(*args)

def test_financial_thesis_has_typed_values_for_answer_verification():
    p=source(growth=40.909);row=score_candidate(p,DAY)
    numeric=row['financial_evidence']
    assert numeric and numeric[0]['value']==40.91 and numeric[0]['unit']=='%'
    assert all(e['quality_status']=='verified' and e['period_end']=='2026-06-30' for e in numeric)

def samples(n=280):
    out=[];first=dt.date(2024,1,1)
    for i in range(n):
        for stock in range(8):
            x=[np.sin((i+1)*(j+1)+stock) for j in range(5)]
            out.append({'stock_code':str(stock),'signal_date':str(first+dt.timedelta(days=i)),
                'exit_date':str(first+dt.timedelta(days=i+5)),'x':x,'y':x[0]*.01})
    return out

def test_reconstructed_research_separates_train_choice_from_test_outcomes():
    rows=samples();plan=history_plan(126,40,2,5)
    a=evaluate_rows(rows,5,plan)
    assert a['status']=='complete' and a['production_approved'] is False
    start=a['folds'][-1]['test_start']
    changed=[{**r,'y':-r['y']} if r['signal_date']>=start else r for r in rows]
    b=evaluate_rows(changed,5,plan)
    assert a['folds'][-1]['chosen_expression']==b['folds'][-1]['chosen_expression']
    assert a['folds'][-1]['direction']==b['folds'][-1]['direction']
    assert a['folds'][-1]['test_rank_ic']==-b['folds'][-1]['test_rank_ic']

def test_reconstruction_short_history_is_explicit_not_an_automatic_lower_threshold():
    r=evaluate_rows(samples(100),20,history_plan(126,40,2,20))
    assert r['status']=='insufficient_history' and r['missing_sessions']==146 and not r['folds']

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(f) for n,f in sorted(globals().items()) if n.startswith('test_') and callable(f))

if __name__=='__main__':unittest.main()
