"""Runner selection and independent stages; subprocesses always mocked."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
# Pipeline and analytics have distinct common.py modules: isolate this import.
spec=importlib.util.spec_from_file_location('pipeline_common_runner_test',ROOT/'scripts/pipeline/common.py')
common=importlib.util.module_from_spec(spec);spec.loader.exec_module(common)
old=sys.modules.get('common');sys.modules['common']=common
try:
 spec=importlib.util.spec_from_file_location('research_intelligence_runner_test',ROOT/'scripts/pipeline/research_intelligence_refresh.py')
 runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
finally:
 if old is None:sys.modules.pop('common',None)
 else:sys.modules['common']=old

CUT=dt.datetime(2026,10,13,16,35,tzinfo=dt.timezone.utc)
DAY=dt.date(2026,10,13)
RUN={'run_id':'focus-run','as_of_date':dt.date(2026,10,14),'decision_cutoff':CUT-dt.timedelta(minutes=4),
     'created_at':CUT-dt.timedelta(minutes=3)}

def stock(code,state='active',priority=50,**extra):
 return {'revision_id':'r-'+code,'focus_key':'stock:'+code,'entity_type':'stock','label':'fixture',
   'state':state,'priority':priority,'stock_codes':[code],'created_at':CUT-dt.timedelta(minutes=2),**extra}

def prepared():
 return {'learning_date':DAY,'focus_refresh_date':dt.date(2026,10,14),
  'selection':runner.build_candidate_selection(RUN,[stock('002203')],CUT),'preflight_errors':[]}

class SelectionTests(unittest.TestCase):
 def test_preserves_nine_references_adds_at_most_eleven(self):
  result=runner.build_candidate_selection(RUN,[stock(f'{2000+i:06d}') for i in range(30)],CUT)
  self.assertEqual(result['reference_codes'],list(runner.DEFAULT_CODES));self.assertEqual(result['selected_count'],20)
  self.assertEqual(len(result['focus_codes']),11);self.assertEqual(len(set(result['stock_codes'])),20)
 def test_direct_company_priority_and_no_theme_expansion(self):
  rows=[stock('002003','watching',99),stock('002002','active',40),stock('002001','active',60),
        stock('003333',entity_type='theme',focus_key='theme:optical',stock_codes=['003333','004444'])]
  result=runner.build_candidate_selection(RUN,rows,CUT)
  self.assertEqual(result['focus_codes'],['002001','002002','002003']);self.assertNotIn('003333',result['stock_codes'])
 def test_reference_overlap_does_not_use_additional_slot(self):
  rows=[stock(runner.DEFAULT_CODES[0]),*[stock(f'{2000+i:06d}') for i in range(12)]]
  result=runner.build_candidate_selection(RUN,rows,CUT);self.assertEqual(len(result['focus_codes']),11)
 def test_cutoff_beijing_day_can_see_today_focus_while_market_is_yesterday(self):
  result=runner.build_candidate_selection(RUN,[stock('002203')],CUT)
  self.assertEqual(result['source_focus_run']['as_of_date'],'2026-10-14');self.assertEqual(result['focus_date_ceiling'],'2026-10-14')
  commands=dict(runner.stage_commands(DAY,learning_date=DAY,stock_codes=result['stock_codes'],focus_date=dt.date(2026,10,14)))
  self.assertEqual(commands['learning'][commands['learning'].index('--as-of')+1],'2026-10-13')
  self.assertEqual(commands['focus'][-1],'2026-10-14')
 def test_run_recorded_after_cutoff_is_never_backdated(self):
  with self.assertRaises(ValueError):runner.build_candidate_selection({**RUN,'created_at':CUT+dt.timedelta(seconds=1)},[],CUT)
  with self.assertRaises(ValueError):runner.build_candidate_selection({**RUN,'decision_cutoff':CUT+dt.timedelta(seconds=1)},[],CUT)
 def test_future_revision_and_cooling_are_not_selected(self):
  result=runner.build_candidate_selection(RUN,[stock('002001',created_at=CUT+dt.timedelta(seconds=1)),stock('002002','cooling')],CUT)
  self.assertEqual(result['stock_codes'],list(runner.DEFAULT_CODES))
 def test_identity_conflict_is_explicit_partial_not_expanded(self):
  result=runner.build_candidate_selection(RUN,[stock('002001',stock_codes=['002001','002002'])],CUT)
  self.assertEqual(result['status'],'partial');self.assertEqual(result['selected_count'],9);self.assertEqual(len(result['errors']),1)
 def test_missing_visible_run_is_unknown_not_empty_success(self):
  result=runner.build_candidate_selection(None,[],CUT);self.assertEqual(result['status'],'unknown');self.assertEqual(result['selected_count'],9)
 def test_read_failure_preserves_reference_sample_and_records_error(self):
  with patch.object(runner,'read_focus_candidates',side_effect=TimeoutError('private SQL')):
   result=runner.select_learning_candidates(CUT)
  self.assertEqual(result['status'],'error');self.assertEqual(result['selected_count'],9)
  self.assertEqual(result['errors'][0]['error_type'],'TimeoutError');self.assertNotIn('private SQL',json.dumps(result))
 def test_bounded_source_is_reported(self):
  rows=[stock(f'{2000+i:06d}',visible_rows_total=100) for i in range(70)]
  result=runner.build_candidate_selection(RUN,rows,CUT);self.assertEqual(result['focus_rows_examined'],64)
  self.assertTrue(result['source_truncated']);self.assertEqual(result['selected_count'],20)

class DynamicPoolSelectionTests(unittest.TestCase):
 def test_ranked_pool_replaces_fixed_references_with_bounded_unique_codes(self):
  rows=[{'stock_code':f'{n:06}','rank':n} for n in range(30,0,-1)]
  result=runner.build_candidate_selection({**RUN,'source':'candidate_pool'},rows+rows,CUT)
  self.assertEqual(result['stock_codes'],[f'{n:06}' for n in range(1,21)])
  self.assertEqual(result['reference_codes'],[]);self.assertTrue(result['source_truncated'])
 def test_future_pool_not_backdated_and_empty_pool_not_success(self):
  for fields in ({'as_of_date':dt.date(2026,10,15)},{'created_at':CUT+dt.timedelta(seconds=1)}):
   with self.assertRaises(ValueError):runner.build_candidate_selection({**RUN,'source':'candidate_pool',**fields},[],CUT)
  result=runner.build_candidate_selection({**RUN,'source':'candidate_pool'},[],CUT)
  self.assertEqual(result['status'],'unknown');self.assertEqual(result['stock_codes'],[])

class IndependentStageTests(unittest.TestCase):
 def test_calendar_preflight_failure_still_executes_focus(self):
  with patch.object(runner,'completed_learning_date',side_effect=ValueError('missing calendar')),patch.object(
    runner,'select_learning_candidates',return_value=prepared()['selection']):
   prep=runner.prepare(DAY,'all',CUT)
  with patch.object(runner,'run_logged') as run,patch.object(runner,'log_path',return_value=Path('/tmp/unused')):
   outcomes,code=runner.execute_stages(DAY,'all',prep)
  self.assertEqual(code,1);self.assertEqual([r['stage'] for r in outcomes],['learning','focus'])
  self.assertFalse(outcomes[0]['command_started']);self.assertEqual(run.call_count,1)
  self.assertIn('update_research_focus.py',run.call_args.args[0][1]);self.assertEqual(run.call_args.args[0][-1],'2026-10-14')
 def test_learning_child_failure_does_not_skip_focus(self):
  with patch.object(runner,'run_logged',side_effect=[subprocess.CalledProcessError(3,'mock'),None]) as run,patch.object(runner,'log_path',return_value=Path('/tmp/unused')):
   outcomes,code=runner.execute_stages(DAY,'all',prepared())
  self.assertEqual(code,1);self.assertEqual([r['exit_code'] for r in outcomes],[3,0]);self.assertEqual(run.call_count,2)
 def test_selection_failure_uses_nine_but_keeps_overall_nonzero(self):
  prep=prepared();prep['selection']=runner.base_selection(CUT);prep['selection'].update(status='error',errors=[{'reason':'read_failed'}])
  with patch.object(runner,'run_logged') as run,patch.object(runner,'log_path',return_value=Path('/tmp/unused')):
   outcomes,code=runner.execute_stages(DAY,'all',prep)
  self.assertEqual(code,1);self.assertEqual([r['exit_code'] for r in outcomes],[0,0])
  cmd=run.call_args_list[0].args[0];self.assertEqual(cmd[cmd.index('--stock-codes')+1],','.join(runner.DEFAULT_CODES))
 def test_order_and_selected_stock_codes_are_explicit(self):
  with patch.object(runner,'run_logged') as run,patch.object(runner,'log_path',return_value=Path('/tmp/unused')):
   outcomes,code=runner.execute_stages(DAY,'all',prepared())
  self.assertEqual(code,0);self.assertEqual([r['stage'] for r in outcomes],['learning','focus'])
  self.assertIn('002203',run.call_args_list[0].args[0][-1])
 def test_focus_only_does_not_query_learning_dependencies(self):
  with patch.object(runner,'completed_learning_date') as calendar,patch.object(runner,'select_learning_candidates') as selector:
   prep=runner.prepare(DAY,'focus',CUT)
  calendar.assert_not_called();selector.assert_not_called();self.assertIsNone(prep['selection'])
 def test_selection_report_cannot_overwrite_a_previous_run(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'immutable.json';runner.write_immutable(p,{'selection':['600000']})
   with self.assertRaises(FileExistsError):runner.write_immutable(p,{'selection':['600036']})
   self.assertEqual(json.loads(p.read_text())['selection'],['600000'])

if __name__=='__main__':unittest.main()
