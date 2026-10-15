from __future__ import annotations
import copy
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from scripts.analytics.factor_validation import VALIDATION_VERSION

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('factor_governance_helper',ROOT/'MMClaw/mmclaw/finance_db_helper.py')
helper=importlib.util.module_from_spec(spec);assert spec and spec.loader;spec.loader.exec_module(helper)
CUTOFF=dt.datetime(2026,1,20,8,tzinfo=dt.timezone.utc)

class Result:
    def __init__(self,rows=None,one=None):self.rows=rows or [];self.one=one
    def fetchall(self):return copy.deepcopy(self.rows)
    def fetchone(self):return copy.deepcopy(self.one)

class GovernanceConnection:
    def __init__(self,row,windows):self.row=row;self.windows=windows;self.calls=[]
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def execute(self,sql,params=None):
        self.calls.append((sql,params))
        if sql.lstrip().startswith('SET '):return Result()
        if 'SELECT max(h.dt) AS dt' in sql:return Result(one={'dt':CUTOFF.date()})
        if sql.lstrip().startswith('SELECT e.*,r.test_start'):return Result(self.windows)
        if 'analytics.forecast_models' in sql:return Result()
        return Result([self.row] if self.row else [])

class FactorToolGovernanceTests(unittest.TestCase):
    def fixture(self):
        windows=[]
        for start,end in [('2025-01-01','2025-02-01'),('2025-04-01','2025-05-01'),('2025-07-01','2025-08-01')]:
            windows.append(dict(factor_id='test-factor',test_start=dt.date.fromisoformat(start),test_end=dt.date.fromisoformat(end),
                horizon_days=20,status='complete',run_status='complete',passed_gates=True,rank_ic_mean=.05,
                metrics=dict(validation_version=VALIDATION_VERSION,input_contract_version='pit-input-v1',strict_pit_ready=True,
                    cost_adjusted=True,execution_constraints='daily-bars',net_spread_mean=.03,factor_version=2,outcome_end=end)))
        row={**copy.deepcopy(windows[-1]),'version':2,'factor_name':'test','display_name':'Test','lifecycle_status':'production',
             'health_date':CUTOFF.date(),'latest_market_date':CUTOFF.date(),'latest_coverage':.95,'raw_stddev':.5,'zero_value_ratio':.05,
             'turnover_mean':.1,'top_bottom_spread_mean':.05,'latest_approval_decision':'approved','approved_by':'human-reviewer',
             'approval_factor_version':2,'approval_created_at':CUTOFF-dt.timedelta(days=1),'effective_date':CUTOFF.date(),
             'approval_expires_at':CUTOFF+dt.timedelta(days=30),'created_at':CUTOFF-dt.timedelta(days=2),'zscore':1.5}
        return row,windows

    def run_tools(self,row,windows,eligible):
        for name in ('run_factor_readiness','run_factor_attribution','run_v3_factor_validation'):
            with self.subTest(tool=name):
                conn=GovernanceConnection(row,windows)
                with patch.object(helper,'connect',return_value=conn),patch.object(helper,'emit') as emit,patch.object(helper,'emit_v3') as emit_v3:
                    getattr(helper,name)(str(ROOT),{'stock_code':'000001','decision_cutoff':CUTOFF.isoformat()})
                output=(emit_v3 if name=='run_v3_factor_validation' else emit).call_args.args[0][0]
                self.assertEqual(output['prediction_eligible'],eligible,output['blockers'])
                if name=='run_v3_factor_validation':self.assertEqual(output['production_approved'],eligible)
                self.assertEqual(output['required_validation_version'],VALIDATION_VERSION)

    def test_three_current_independent_windows_with_current_human_approval_pass(self):
        row,windows=self.fixture();self.run_tools(row,windows,True)

    def test_legacy_validation_and_legacy_factor_version_never_pass(self):
        for field,value in [('validation_version','purged-cost-v2'),('factor_version',1),('strict_pit_ready',False)]:
            with self.subTest(field=field):
                row,windows=self.fixture();row['metrics'][field]=value
                for w in windows:w['metrics'][field]=value
                self.run_tools(row,windows,False)

    def test_latest_failure_cannot_be_hidden_by_three_old_passing_windows(self):
        row,windows=self.fixture();row['passed_gates']=False;self.run_tools(row,windows,False)

    def test_approval_requires_version_actor_effective_date_and_expiry(self):
        for field,value in [('approval_factor_version',1),('approved_by',''),('latest_approval_decision','revoked'),
                            ('approval_created_at',CUTOFF+dt.timedelta(seconds=1)),
                            ('effective_date',CUTOFF.date()+dt.timedelta(days=1)),('approval_expires_at',CUTOFF)]:
            with self.subTest(field=field):
                row,windows=self.fixture();row[field]=value;self.run_tools(row,windows,False)

    def test_windows_cannot_mix_horizons_or_overlap_label_periods(self):
        row,windows=self.fixture();windows[0]['horizon_days']=5;self.run_tools(row,windows,False)
        row,windows=self.fixture();windows[0]['metrics']['outcome_end']='2025-04-20';self.run_tools(row,windows,False)

    def test_health_and_supported_horizon_are_required(self):
        for field,value in [('health_date',CUTOFF.date()-dt.timedelta(days=1)),('latest_coverage',.79),('zero_value_ratio',.99),('horizon_days',1)]:
            with self.subTest(field=field):
                row,windows=self.fixture();row[field]=value;self.run_tools(row,windows,False)

    def test_one_day_request_never_expands_into_unfiltered_history(self):
        with patch.object(helper,'connect') as connect,patch.object(helper,'emit') as emit:
            helper.run_factor_validation(str(ROOT),{'horizons':[1]})
        connect.assert_not_called();self.assertEqual(emit.call_args.args[0],[])
        self.assertIn('T+1',emit.call_args.args[1]['error'])

    def test_default_horizons_and_mixed_requests_exclude_one_day_include_sixty(self):
        for horizons in (None,[1,5,20,60]):
            conn=GovernanceConnection(None,[])
            with patch.object(helper,'connect',return_value=conn),patch.object(helper,'emit') as emit:
                helper.run_factor_validation(str(ROOT),{} if horizons is None else {'horizons':horizons})
            self.assertEqual(emit.call_args.args[1]['horizons'],[5,20,60])
            self.assertEqual(conn.calls[-1][1]['horizons'],[5,20,60])
            self.assertTrue(conn.calls[-1][1]['has_horizons'])

if __name__=='__main__':unittest.main()
