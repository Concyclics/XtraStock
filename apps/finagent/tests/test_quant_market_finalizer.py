import copy,fcntl,hashlib,importlib.util,io,json,sqlite3,sys,tempfile,unittest
from contextlib import nullcontext,redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock,patch
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/pipeline'))
from finalize_quant_market_repair import queue_evidence,graph_evidence,readiness_from_log,plan
import finalize_quant_market_repair as finalizer
from market_gap_repair import ledger
class FinalizerContractTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
 def tearDown(self):self.tmp.cleanup()
 def test_exhausted_is_terminal_but_not_successful(self):
  c=ledger(self.root/'queue.sqlite3')
  c.execute("INSERT INTO jobs(asset_id,secid,dates,status,attempts,updated_at,last_error) VALUES('x','0.000001','[]','exhausted',3,1,'missing day')");c.commit();c.close()
  got=queue_evidence(self.root);self.assertTrue(got['ready']);self.assertFalse(got['all_successful']);self.assertEqual(got['unresolved'][0]['last_error'],'missing day')
 def test_empty_missing_and_running_queues_not_ready(self):
  self.assertFalse(queue_evidence(self.root)['ready']);c=ledger(self.root/'queue.sqlite3');self.assertFalse(queue_evidence(self.root)['ready'])
  c.execute("INSERT INTO jobs(asset_id,secid,dates,status,attempts,updated_at) VALUES('x','0.000001','[]','running',1,1)");c.commit();c.close();self.assertFalse(queue_evidence(self.root)['ready'])
 def test_graph_directory_or_wrong_mode_is_not_completion(self):
  p=self.root/'graph.json';self.assertFalse(graph_evidence(p)['ready'])
  data={'status':'complete','at':'2026-10-13T14:45:00+00:00','deployment_files':['x'],'rebuilt_date':'2026-10-12','knowledge_mode':'system','published_research_snapshots_modified':False}
  p.write_text(json.dumps(data));self.assertFalse(graph_evidence(p)['ready']);data['knowledge_mode']='public';p.write_text(json.dumps(data));self.assertTrue(graph_evidence(p)['ready'])
 def test_failed_graph_stops_chain(self):
  p=self.root/'graph.json';p.write_text('{"status":"failed"}')
  with self.assertRaisesRegex(RuntimeError,'graph finalizer failed'):graph_evidence(p)
 def test_actual_readiness_json_contract(self):
  p=self.root/'ready.log';p.write_text('progress\n'+json.dumps({'date':'2026-10-13','mode':'paper','readiness':'blocked','checks':[{'passed':False}]}))
  self.assertEqual(readiness_from_log(p)[0],'blocked')
  p.write_text('no result\n')
  with self.assertRaisesRegex(RuntimeError,'no ready/blocked'):readiness_from_log(p)
 def test_plan_enforces_causal_order_and_no_quality_bypass(self):
  tasks=plan('2026-08-01','2026-10-13','2026-10-13');names=[t['name'] for t in tasks]
  self.assertLess(names.index('canonical'),names.index('technical'));self.assertLess(names.index('system_graph'),names.index('features'));self.assertLess(names.index('features'),names.index('scores'));self.assertLess(names.index('scores'),names.index('snapshot'))
  args=' '.join(' '.join(t['command']) for t in tasks);self.assertNotIn('--allow-quality-fail',args);self.assertIn('--knowledge-mode system',args);self.assertIn('--readiness-only',args)
class AuditedReuseTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.old=self.root/'old';self.old.mkdir();(self.old/'finalizer.lock').touch()
  self.path=self.old/'checkpoint.json';self.new=self.root/'new';self.market=self.root/'market';self.market.mkdir()
  self.tasks=[]
  for name in ('canonical','scores','readiness'):
   script=self.root/(name+'.py');script.write_text('# original '+name)
   self.tasks.append({'name':name,'command':['python',str(script)],'script_sha256':hashlib.sha256(script.read_bytes()).hexdigest()})
  self.market_e={'ready':True,'fingerprint':'a'*64};self.graph_e={'ready':True,'sha256':'b'*64}
  success={'name':'canonical','attempt':1,'command':self.tasks[0]['command'],'returncode':0,'started_at':'2026-10-13T12:00:00+00:00','finished_at':'2026-10-13T12:01:00+00:00','log':str(self.old/'canonical.log')}
  self.source={'version':finalizer.VERSION,'status':'stopped','range':['2026-08-01','2026-10-13'],'as_of':'2026-10-13',
   'steps':self.tasks,'plan_sha256':finalizer.plan_hash(self.tasks),'completed':['canonical'],
   'attempts':[success,{'name':'scores','attempt':1,'command':self.tasks[1]['command'],'returncode':-15,'finished_at':'2026-10-13T13:00:00+00:00'}],
   'market':self.market_e,'graph':self.graph_e,'dependency_sha256':finalizer.dependency_hash(self.market_e,self.graph_e),
   'error':'InterruptedError: finalizer received signal 15','finished_at':'2026-10-13T13:00:00+00:00'}
  self.save()
 def tearDown(self):self.tmp.cleanup()
 def save(self):self.path.write_text(json.dumps(self.source))
 def validate(self,tasks=None):return finalizer.validate_reuse_checkpoint(self.path,tasks or self.tasks,'2026-08-01','2026-10-13','2026-10-13')
 def args(self):return SimpleNamespace(state_dir=self.new,resume=False,since='2026-08-01',until='2026-10-13',as_of='2026-10-13',wait_seconds=1,step_timeout_seconds=30,market_state_dir=self.market,graph_completion=self.root/'graph.json')
 def test_only_successful_unchanged_prefix_is_inherited(self):
  changed=copy.deepcopy(self.tasks);changed[1]['script_sha256']='c'*64
  before=self.path.read_bytes();reuse=self.validate(changed)
  self.assertEqual(reuse['completed'],['canonical']);self.assertEqual(len(reuse['receipts']),1)
  self.assertEqual(reuse['audit']['source_attempts'][-1]['returncode'],-15)
  self.assertEqual(reuse['receipts'][0]['script_sha256_basis'],'source_plan_v1_runtime_checked')
  self.assertEqual(reuse['source_bytes'],before);self.assertEqual(self.path.read_bytes(),before)
 def test_nonterminal_or_completed_source_is_rejected(self):
  for status in ('running','waiting_dependencies','complete_ready','completed_data_rebuild_blocked_research'):
   with self.subTest(status=status):
    self.source['status']=status;self.save()
    with self.assertRaisesRegex(RuntimeError,'terminal stopped/failed'):self.validate()
 def test_range_version_asof_and_plan_integrity_are_checked(self):
  original=copy.deepcopy(self.source)
  for field,value in [('version','old'),('range',['2026-09-01','2026-10-13']),('as_of','2026-10-12'),('plan_sha256','invalid')]:
   self.source=copy.deepcopy(original);self.source[field]=value;self.save()
   with self.subTest(field=field),self.assertRaises(RuntimeError):self.validate()
 def test_holes_duplicates_and_unknown_completed_stages_fail(self):
  for completed in (['scores'],['canonical','readiness'],['canonical','canonical'],['unknown']):
   self.source['completed']=completed;self.save()
   with self.subTest(completed=completed),self.assertRaisesRegex(RuntimeError,'exact plan prefix'):self.validate()
 def test_changed_completed_code_or_command_cannot_be_reused(self):
  for key,value in [('script_sha256','d'*64),('command',['python','different.py'])]:
   tasks=copy.deepcopy(self.tasks);tasks[0][key]=value
   with self.subTest(key=key),self.assertRaisesRegex(RuntimeError,'completed stage command/script changed'):self.validate(tasks)
 def test_latest_attempt_must_be_finished_success_with_matching_command(self):
  original=copy.deepcopy(self.source)
  for changes in ({'returncode':-15},{'returncode':False},{'finished_at':None},{'command':['different']},{'script_sha256':'d'*64}):
   self.source=copy.deepcopy(original);self.source['attempts'].append({**self.source['attempts'][0],**changes});self.save()
   with self.subTest(changes=changes),self.assertRaises(RuntimeError):self.validate()
  self.source=copy.deepcopy(original);self.source['attempts']=self.source['attempts'][1:];self.save()
  with self.assertRaisesRegex(RuntimeError,'last successful attempt'):self.validate()
 def test_dependency_change_or_rewritten_source_fails(self):
  reuse=self.validate()
  finalizer.verify_reuse_dependencies(reuse,self.market_e,self.graph_e)
  with self.assertRaisesRegex(RuntimeError,'dependency evidence changed'):
   finalizer.verify_reuse_dependencies(reuse,{'ready':True,'fingerprint':'c'*64},self.graph_e)
  with self.assertRaisesRegex(RuntimeError,'terminal market'):
   finalizer.verify_reuse_dependencies(reuse,{'ready':False},self.graph_e)
  self.path.write_text(self.path.read_text()+'\n')
  with self.assertRaisesRegex(RuntimeError,'source checkpoint changed'):
   finalizer.verify_reuse_dependencies(reuse,self.market_e,self.graph_e)
 def test_existing_destination_or_held_source_lock_fails(self):
  self.new.mkdir()
  with self.assertRaisesRegex(RuntimeError,'nonexistent'):
   with finalizer.reusable_source(self.path,self.new,self.tasks,'2026-08-01','2026-10-13','2026-10-13'):pass
  self.new.rmdir()
  with (self.old/'finalizer.lock').open('rb') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with self.assertRaisesRegex(RuntimeError,'still running/locked'):
    with finalizer.reusable_source(self.path,self.new,self.tasks,'2026-08-01','2026-10-13','2026-10-13'):pass
 def test_plan_only_validates_without_creating_or_altering_checkpoints(self):
  before=self.path.read_bytes();out=io.StringIO()
  with patch.object(sys,'argv',['finalizer','--state-dir',str(self.new),'--reuse-completed-from',str(self.path)]),patch.object(finalizer,'plan',return_value=self.tasks),patch.object(finalizer,'queue_evidence',return_value=self.market_e),patch.object(finalizer,'graph_evidence',return_value=self.graph_e),redirect_stdout(out):
   self.assertEqual(finalizer.main(),0)
  result=json.loads(out.getvalue());self.assertEqual(result['inherited_completed'],['canonical'])
  self.assertEqual(result['remaining_steps'],['scores','readiness']);self.assertFalse(self.new.exists());self.assertEqual(before,self.path.read_bytes())
 def test_execution_skips_inherited_stage_and_keeps_original_failure(self):
  reuse=self.validate();before=self.path.read_bytes()
  def popen(command,**kwargs):
   if command==self.tasks[-1]['command']:kwargs['stdout'].write('{"readiness":"blocked"}\n');kwargs['stdout'].flush()
   return Mock(wait=Mock(return_value=0))
  with patch.object(finalizer,'queue_evidence',return_value=self.market_e),patch.object(finalizer,'graph_evidence',return_value=self.graph_e),patch.object(finalizer,'schema_preflight',return_value={}),patch.object(finalizer,'acquire_pipeline_locks',return_value=nullcontext()),patch.object(finalizer.signal,'signal'),patch.object(finalizer.subprocess,'Popen',side_effect=popen) as child,redirect_stdout(io.StringIO()):
   self.assertEqual(finalizer.run_chain(self.args(),self.tasks,finalizer.plan_hash(self.tasks),reuse),0)
  self.assertEqual([c.args[0] for c in child.call_args_list],[s['command'] for s in self.tasks[1:]])
  state=json.loads((self.new/'checkpoint.json').read_text());self.assertEqual([a['name'] for a in state['attempts']],['scores','readiness'])
  self.assertEqual(state['reuse_completed_from']['source_attempts'][-1]['returncode'],-15)
  self.assertEqual((self.new/'inherited-source-checkpoint.json').read_bytes(),before);self.assertEqual(self.path.read_bytes(),before)
 def test_dependency_rechecked_after_waiting_locks_before_any_child(self):
  reuse=self.validate();changed={**self.graph_e,'sha256':'d'*64}
  with patch.object(finalizer,'queue_evidence',return_value=self.market_e),patch.object(finalizer,'graph_evidence',side_effect=[self.graph_e,changed]),patch.object(finalizer,'schema_preflight',return_value={}),patch.object(finalizer,'acquire_pipeline_locks',return_value=nullcontext()),patch.object(finalizer.signal,'signal'),patch.object(finalizer.subprocess,'Popen') as child,redirect_stdout(io.StringIO()):
   self.assertEqual(finalizer.run_chain(self.args(),self.tasks,finalizer.plan_hash(self.tasks),reuse),1)
  child.assert_not_called();state=json.loads((self.new/'checkpoint.json').read_text());self.assertEqual(state['status'],'failed')
 def test_original_resume_still_rejects_any_changed_plan(self):
  args=self.args();args.state_dir=self.old;args.resume=True
  with self.assertRaisesRegex(RuntimeError,'plan/script changed'):
   finalizer.run_chain(args,self.tasks,'changed-plan')

if __name__=='__main__':unittest.main()
