"""Snapshot factor approval must survive its own time-valid governance gates."""
import copy
import datetime as dt
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'/'analytics'))
from build_research_snapshot import factor_eligibility
from factor_validation import VALIDATION_VERSION
UTC=dt.timezone.utc
CUTOFF=dt.datetime(2026,10,13,16,10,tzinfo=UTC)  # already 14 October in Beijing
AS_OF=dt.date(2026,10,13)


class SnapshotFactorGovernanceTests(unittest.TestCase):
    def fixture(self):
        d={'version':2,'lifecycle_status':'production'}
        windows=[]
        for start,end in [('2025-07-01','2025-08-01'),('2025-04-01','2025-05-01'),('2025-01-01','2025-02-01')]:
            windows.append({'test_start':start,'test_end':end,'horizon_days':20,'status':'complete',
                'passed_gates':True,'rank_ic_mean':.05,
                'metrics':{'validation_version':VALIDATION_VERSION,'input_contract_version':'pit-input-v1',
                    'strict_pit_ready':True,'cost_adjusted':True,'execution_constraints':'daily-bars',
                    'net_spread_mean':.03,'factor_version':2,'outcome_end':end}})
        approval={'decision':'approved','approved_by':'human-reviewer','factor_version':2,
                  'created_at':CUTOFF-dt.timedelta(days=1),'effective_date':AS_OF,'expires_at':None}
        health={'dt':AS_OF,'computed_at':CUTOFF,'latest_available_at':CUTOFF,
                'coverage':.8,'raw_stddev':.5,'zero_value_ratio':.05}
        return d,windows,approval,health

    def eligible(self,fixture):
        return factor_eligibility(*fixture,AS_OF,CUTOFF)['prediction_eligible']

    def test_current_named_manual_approval_and_known_healthy_inputs_pass(self):
        self.assertTrue(self.eligible(self.fixture()))

    def test_missing_blank_future_expired_or_revoked_approval_does_not_pass(self):
        changes=[('approved_by',None),('approved_by','  '),('factor_version',1),
                 ('decision','revoked'),('decision','rejected'),
                 ('created_at',CUTOFF+dt.timedelta(microseconds=1)),('created_at',None),
                 ('effective_date',dt.date(2026,10,15)),('effective_date',None),
                 ('expires_at',CUTOFF),('expires_at',CUTOFF-dt.timedelta(days=1))]
        for field,value in changes:
            with self.subTest(field=field,value=value):
                f=self.fixture();f[2][field]=value
                self.assertFalse(self.eligible(f))

    def test_approval_effective_date_uses_actual_beijing_decision_date(self):
        f=self.fixture();f[2]['effective_date']=dt.date(2026,10,14)
        self.assertTrue(self.eligible(f))
        f[2]['expires_at']=CUTOFF+dt.timedelta(microseconds=1)
        self.assertTrue(self.eligible(f))

    def test_later_health_or_source_cannot_confer_earlier_approval(self):
        for field in ('computed_at','latest_available_at'):
            for value in (None,CUTOFF+dt.timedelta(microseconds=1)):
                with self.subTest(field=field,value=value):
                    f=self.fixture();f[3][field]=value
                    self.assertFalse(self.eligible(f))
        f=self.fixture();f[3]['dt']=AS_OF-dt.timedelta(days=1)
        self.assertFalse(self.eligible(f))

    def test_unknown_or_nonfinite_health_does_not_mean_healthy(self):
        for field in ('coverage','raw_stddev','zero_value_ratio'):
            for value in (None,float('nan'),float('inf')):
                with self.subTest(field=field,value=value):
                    f=self.fixture();f[3][field]=value
                    self.assertFalse(self.eligible(f))
        for field,value in [('coverage',.799),('raw_stddev',0),('zero_value_ratio',.99)]:
            f=self.fixture();f[3][field]=value
            self.assertFalse(self.eligible(f))

    def test_three_old_passes_cannot_hide_latest_failure(self):
        f=self.fixture();latest=copy.deepcopy(f[1][0]);latest['passed_gates']=False
        f[1].insert(0,latest)
        result=factor_eligibility(*f,AS_OF,CUTOFF)
        self.assertEqual(result['passing_independent_windows'],3)
        self.assertFalse(result['prediction_eligible'])

    def test_shadow_or_legacy_evidence_cannot_inherit_approval(self):
        f=self.fixture();f[0]['lifecycle_status']='shadow';self.assertFalse(self.eligible(f))
        for field,value in [('validation_version','purged-cost-v2'),('factor_version',1),('strict_pit_ready',False)]:
            f=self.fixture()
            for row in f[1]:row['metrics'][field]=value
            self.assertFalse(self.eligible(f))


if __name__=='__main__':unittest.main()
