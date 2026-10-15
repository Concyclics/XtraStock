"""Latest failed refresh must not make older models or unknown input gaps look fresh."""
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
sys.path.insert(0,str(APP/'scripts'/'analytics'))
import learning_state as state
from scripts.analytics.research_intelligence import intelligence_rows

NOW=dt.datetime(2026,10,14,1,tzinfo=dt.timezone.utc)
DAY=dt.date(2026,10,13)


class Result:
    def __init__(self,rows):self.rows=rows
    def fetchone(self):return self.rows[0] if self.rows else None
    def fetchall(self):return self.rows


class Connection:
    def __init__(self,run,models=()):self.run=run;self.models=models;self.calls=[]
    def execute(self,sql,params=None):
        self.calls.append(sql)
        if 'to_regclass' in sql:return Result([{'t':'analytics.learning_models_v1'}])
        if 'max(trade_date)' in sql:return Result([{'day':DAY}])
        if 'learning_cycles_v1' in sql:return Result([self.run] if self.run else [])
        if 'SELECT DISTINCT ON(horizon)' in sql:return Result(self.models)
        if 'learning_predictions_v1' in sql:return Result([])
        raise AssertionError('unexpected or mutating SQL')


def failure(**extra):
    return {'status':'failed','completed_at':NOW,'summary':{
        'failure_phase':'model_horizon_20_training','error_type':'RuntimeError',
        'error':'SECRET host credentials and SQL must not be displayed',
        'committed_progress':{'model_revisions_added':1,'models_reused':0,
            'predictions_added':3,'outcome_revisions_added':2},**extra}}


class FailureDisplayTests(unittest.TestCase):
    def test_committed_work_and_failed_phase_remain_visible_without_raw_errors(self):
        result=state.compact_update_failure(failure())
        self.assertTrue(result['partial_cycle_writes'])
        self.assertEqual(result['committed_progress']['model_revisions_added'],1)
        self.assertEqual(result['committed_progress']['predictions_added'],3)
        self.assertIn('20个交易日',result['phase_text'])
        self.assertNotIn('SECRET',str(result))
        self.assertFalse(result['production_approved'])

    def test_legacy_failure_without_progress_is_unknown_not_zero(self):
        result=state.compact_update_failure({'status':'failed','summary':{}})
        self.assertIsNone(result['partial_cycle_writes'])
        self.assertTrue(all(v is None for v in result['committed_progress'].values()))

    def test_operational_metadata_cannot_smuggle_raw_errors(self):
        result=state.compact_update_failure(failure(failure_phase='SELECT SECRET',error_type='SECRET password'))
        self.assertEqual(result['phase'],'unknown')
        self.assertEqual(result['error_type'],'UnknownError')
        self.assertNotIn('SECRET',str(result))
        self.assertIsNone(state.compact_update_failure({'status':'run'}))

    def test_latest_failure_preserves_actual_older_model_date_and_unknown_gaps(self):
        model={'horizon':5,'as_of':dt.date(2026,10,12),'created_at':NOW-dt.timedelta(days=1),
            'input_stats':{'rows':700,'dates':650,'stocks':1,'stock_codes':['600036'],'outcomes_through':'2026-09-30'},
            'parameters':{'trained_outcome_through':'2026-08-31'},
            'evaluation':{'research_validation_passed':False,'final_test':{
                'rows':100,'dates':100,'start':'2026-01-01','end':'2026-08-01',
                'mae':.03,'zero_return_mae':.02,'momentum_mae':.04,'coverage_80':.8}}}
        conn=Connection(failure(),[model])
        with patch.object(state,'load_prospective_monitor',return_value={'mature_observations':0,'by_horizon':[]}):
            result=state.load_learning_state(conn,NOW,DAY,['600036'])
        self.assertEqual(result['update_status'],'failed')
        self.assertEqual(result['input_gaps'],[])
        self.assertEqual(result['input_gaps_status'],'unknown_after_failure')
        self.assertEqual(result['research_models'][0]['as_of_market_date'],'2026-10-12')
        self.assertEqual(result['research_models'][0]['updated_at'],str(model['created_at']))
        self.assertTrue(all(sql.lstrip().startswith('SELECT') for sql in conn.calls))
        projected=intelligence_rows({'learning':result})[0]
        self.assertEqual(projected['update_failure'],result['update_failure'])
        self.assertEqual(projected['input_gaps_status'],'unknown_after_failure')

    def test_successful_assessment_clears_failure_but_retains_scope(self):
        conn=Connection({'status':'idempotent','completed_at':NOW,
            'summary':{'models':[{'horizon':5,'current_inputs':{'600036':{'status':'ready'}}}]}})
        with patch.object(state,'load_prospective_monitor',return_value={'by_horizon':[]}):
            result=state.load_learning_state(conn,NOW,DAY,['600036'])
        self.assertIsNone(result['update_failure'])
        self.assertEqual(result['input_gaps_status'],'assessed_in_last_cycle')


if __name__=='__main__':unittest.main()
