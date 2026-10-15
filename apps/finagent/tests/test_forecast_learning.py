import datetime as dt
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'/'analytics'))
from forecast_learning import build_examples, split_four_ways, train_research, settle_prediction, fit, predict, can_record_forward, price_content_digest
UTC=dt.timezone.utc


def fixture(n=900):
    dates=[dt.date(2022,1,1)+dt.timedelta(days=i) for i in range(n+65)]
    calendar=[{'trade_date':day,'open_at':dt.datetime.combine(day,dt.time(1,30),UTC),
               'close_at':dt.datetime.combine(day,dt.time(7),UTC),'is_verified':True} for day in dates]
    cutoff=dt.datetime.combine(dates[n-1],dt.time(15),UTC)
    prices=[{'stock_code':'600036','name':'test','dt':day,'open':100+i*.05,'close':100+i*.05+.01*np.sin(i),
        'raw_close':100+i*.05,'volume':1000+i%17,'quality_status':'unverified','adjustment_quality':'verified',
        'source_available_at':dt.datetime.combine(day,dt.time(8),UTC)} for i,day in enumerate(dates[:n])]
    return prices,calendar,dates[n-1],cutoff


def test_exact_horizon_and_next_open():
    prices,calendar,day,cutoff=fixture(60)
    rows,current,_=build_examples(prices,calendar,5,day,cutoff)
    r=rows[0]
    assert r['entry_date']==str(prices[21]['dt'])
    assert r['exit_date']==str(prices[25]['dt'])
    assert r['y']==prices[25]['close']/prices[21]['open']-1
    assert len(current)==1


def test_missing_day_never_stretches_window():
    p,c,d,t=fixture(60);missing=p.pop(50)['dt']
    _,current,issues=build_examples(p,c,5,d,t)
    assert current==[] and str(missing) in issues['600036']['missing_dates']


def test_bad_adjustment_or_future_availability_blocks():
    p,c,d,t=fixture(60);p[-2]['adjustment_quality']='unverified'
    assert not build_examples(p,c,5,d,t)[1]
    p[-2]['adjustment_quality']='verified';p[-2]['source_available_at']=t+dt.timedelta(seconds=1)
    assert not build_examples(p,c,5,d,t)[1]


def test_unverified_future_calendar_cannot_freeze_forward():
    p,c,d,t=fixture(60);c[61]['is_verified']=False
    rows,current,issues=build_examples(p,c,5,d,t)
    assert rows and not current
    assert issues['600036']['reasons']==['forward_or_feature_calendar_not_verified']


def test_date_grouped_purges_and_four_disjoint_roles():
    p,c,d,t=fixture();rows,_,_=build_examples(p,c,20,d,t)
    parts=split_four_ways(rows)
    prev=None
    for name in ('train','tune','calibration','test'):
        group=parts[name]
        if prev:
            assert max(r['exit_date'] for r in prev)<min(r['signal_date'] for r in group)
        prev=group
    assert len(set(r['signal_date'] for r in parts['test']))>=60


def test_test_labels_cannot_change_tuning_or_calibration():
    p,c,d,t=fixture();rows,_,_=build_examples(p,c,5,d,t)
    before=train_research(rows,5)
    start=split_four_ways(rows)['test'][0]['signal_date']
    changed=[dict(r,y=r['y']+5) if r['signal_date']>=start else dict(r) for r in rows]
    after=train_research(changed,5)
    assert before['evaluation']['selected_alpha']==after['evaluation']['selected_alpha']
    assert before['evaluation']['evaluated_parameters']==after['evaluation']['evaluated_parameters']
    assert before['evaluation']['evaluated_residual_interval_80']==after['evaluation']['evaluated_residual_interval_80']
    assert before['evaluation']['final_test']['mae']!=after['evaluation']['final_test']['mae']
    assert after['evaluation']['production_approved'] is False
    # Refreshed learner actually incorporates newly mature labels, distinctly from frozen evaluation.
    assert before['parameters']!=after['parameters']


def test_training_scaling_does_not_peek_at_test():
    rows=[{'x':[float(i)]*5,'y':i*.01} for i in range(5)]
    model=fit(rows,1)
    assert model['mean']==[2.0]*5
    assert np.isfinite(predict(model,[{'x':[999.0]*5}])).all()


def test_outcome_maturity_and_correction_revision():
    p,c,d,t=fixture(60)
    pred={'stock_code':'600036','entry_date':str(p[30]['dt']),'exit_date':str(p[34]['dt']),
          'horizon':5,'expected_gross_return':.01,'interval_80':[-.1,.1],'momentum_baseline':.005}
    assert settle_prediction(pred,p,c,c[34]['close_at']-dt.timedelta(seconds=1)) is None
    a=settle_prediction(pred,p,c,t);assert a and a['executable_result'] is False
    p[34]['close']*=1.1
    b=settle_prediction(pred,p,c,t)
    assert b['source_digest']!=a['source_digest'] and b['realized_gross_return']!=a['realized_gross_return']
    p.pop(32)
    assert settle_prediction(pred,p,c,t) is None


def test_midnight_weekend_and_open_recording_boundary():
    friday=dt.date(2026,10,9)
    monday_open=dt.datetime(2026,10,12,1,30,tzinfo=UTC)
    assert can_record_forward(friday,friday,monday_open,dt.datetime(2026,10,11,18,tzinfo=UTC))
    assert can_record_forward(friday,friday,monday_open,monday_open-dt.timedelta(seconds=1))
    assert not can_record_forward(friday,friday,monday_open,monday_open)
    assert not can_record_forward(friday,dt.date(2026,10,12),monday_open,monday_open-dt.timedelta(days=1))
    assert not can_record_forward(friday,friday,None,monday_open-dt.timedelta(days=1))


def test_run_started_before_open_but_prediction_finished_after_open_is_rejected():
    signal=dt.date(2026,10,13)
    entry=dt.datetime(2026,10,14,1,30,tzinfo=UTC)
    started=entry-dt.timedelta(seconds=10)
    actual_recorded=entry+dt.timedelta(seconds=1)
    assert can_record_forward(signal,signal,entry,started)
    assert not can_record_forward(signal,signal,entry,actual_recorded)


def test_semantic_digest_ignores_refresh_clock_but_detects_data_corrections():
    p,_,_,t=fixture(60)
    original=price_content_digest(p)
    p[10]['source_available_at']=t
    assert price_content_digest(p)==original
    p[10]['close']+=1
    assert price_content_digest(p)!=original


def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite(unittest.FunctionTestCase(fn) for name,fn in sorted(globals().items())
                              if name.startswith('test_') and callable(fn))
