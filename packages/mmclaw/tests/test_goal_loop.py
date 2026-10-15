import contextlib
import io
import json
import queue
import threading
import unittest
from unittest import mock

from mmclaw.agentic_loop import GoalLoop
from mmclaw.evidence import EvidenceStore
from mmclaw.kernel import MMClaw, StopRequested
from mmclaw.memory import StatelessMemory
from mmclaw.tool_control import ToolLoopController
from mmclaw.tool_schemas import get_native_tool_schemas


CONFIG = {"tool_profile":"finance_chat_readonly", "tool_calling_mode":"json"}
E1 = 'a'*32+':E0001'


def plan(count=2, *, mode="staged", requires_tools=False):
    return {"goal":"完成用户要求的核对与解释", "constraints":["不得添加用户未要求的推荐"], "response_mode":mode,
            "requirements":[{"id":f"R{i}","description":f"用户需求{i}","acceptance":f"交付需求{i}的具体结果",
                             "requires_tools":requires_tools} for i in range(1,count+1)],
            "steps":[{"id":f"S{i}","requirement_ids":[f"R{i}"],"action":f"完成需求{i}",
                      "tools":["finance_market_screen"],"depends_on":[]} for i in range(1,count+1)],
            "selected_tools":["finance_market_screen"]}


def review(states, *, publish=True, decision=None, proof=False):
    return {"decision":decision or ("complete" if all(s=='satisfied' for s in states) else "continue"),
            "publish":publish,
            "candidate_delivery":{"requirement_ids":[f"R{i}" for i,s in enumerate(states,1)
                if s == ('blocked' if decision in {'blocked','needs_input'} else 'satisfied')],
                "self_contained":publish,"kind":'blocker' if decision in {'blocked','needs_input'} else 'result'},
            "checks":[{"id":f"R{i}","status":s,"reason":"已核对候选答复中的结果",
                                         "evidence_ids":[E1] if proof and s=='satisfied' else [],"tool_call_ids":[]}
                                        for i,s in enumerate(states,1)],
            "remaining":[{"requirement_id":f"R{i}","action":f"补做需求{i}","tools":["finance_market_series_batch"]}
                         for i,s in enumerate(states,1) if s!='satisfied'],"missing_requirements":[]}


def response(payload):
    return {"content":json.dumps(payload,ensure_ascii=False)}


class GoalStateTests(unittest.TestCase):
    def setUp(self):
        self.loop=GoalLoop(CONFIG,'How does the evidence compare?')
        self.loop.bind_tools(get_native_tool_schemas(CONFIG,route=False))
        self.loop.accept_plan(plan())
        self.evidence=EvidenceStore('a'*32,self.loop.user_text)
        self.control=ToolLoopController()

    def test_no_topic_or_language_is_required_to_start_planning(self):
        for query in ('Why did the story change?','把这段话改写得更清楚','最近热点有哪些','现在几点'):
            loop=GoalLoop(CONFIG,query)
            self.assertTrue(loop.enabled)
            self.assertEqual(loop.requirements,[])
            self.assertNotIn('sector overview',loop.instruction(self.evidence,self.control))
        generic=GoalLoop({'tool_profile':'default','agentic_loop':{'enabled':True}},'整理实验观察')
        self.assertTrue(generic.enabled)

    def test_explicit_plan_drives_tools_not_question_keywords(self):
        self.assertEqual(self.loop.selected_tools,['finance_market_screen'])
        self.loop.update({'select_tools':['finance_graph_analysis','shell_execute','finance_graph_analysis']})
        self.assertEqual([s['function']['name'] for s in self.loop.selected_schemas()],['finance_graph_analysis'])
        self.assertIn('shell_execute',self.loop.rejected_tools)

    def test_arguments_are_checked_before_python_tool_dispatch(self):
        name='finance_resolve_entities'
        self.assertIn('Unknown arguments',self.loop.argument_error(name,{'query':'中际旭创','entity_type':'stock'}))
        self.assertIsNone(self.loop.argument_error(name,{'query':'中际旭创'}))
        self.assertIn('Invalid type',self.loop.argument_error(name,{'query':['中际旭创']}))
        self.assertIn('permitted',self.loop.argument_error('shell_execute',{}))

    def test_general_profiles_do_not_inherit_finance_default_enablement(self):
        config={'tool_profile':'default','finance_v3':{'agentic_loop':{'enabled':True}}}
        self.assertFalse(GoalLoop(config,'整理文本').enabled)

    def test_catalog_advertises_disclosures_and_default_retrieval_window(self):
        function=self.loop.schemas['finance_document_retrieve']['function']
        self.assertIn('announcements',function['description'])
        props=function['parameters']['properties']
        self.assertIn('announcement',props['source_types']['description'])
        self.assertIn('Default 30',props['days']['description'])

    def test_plan_must_cover_every_requirement_and_cannot_have_cycles(self):
        invalid=plan();invalid['steps']=invalid['steps'][:1]
        with self.assertRaises(ValueError):self.loop.accept_plan(invalid)
        invalid=plan();invalid['steps'][0]['depends_on']=['S2']
        with self.assertRaises(ValueError):self.loop.accept_plan(invalid)

    def test_executor_cannot_mark_task_complete_without_review(self):
        self.loop.update({'requirements_progress':[{'id':'R1','status':'done'}]})
        self.assertEqual(self.loop.requirements[0]['status'],'pending')
        self.assertTrue(self.loop.followup({'task_status':'complete'},self.evidence,self.control))

    def test_reviewer_must_check_all_requirements(self):
        with self.assertRaises(ValueError):self.loop.accept_review(review(['satisfied']),self.evidence,'结论')

    def test_missing_requirement_overrides_premature_complete(self):
        result=self.loop.accept_review(review(['satisfied','missing'],decision='complete'),self.evidence,'只回答一项')
        self.assertEqual(result['decision'],'continue')
        self.assertIn('补做需求2',self.loop.followup({},self.evidence,self.control))
        self.assertEqual(self.loop.selected_tools,['finance_market_series_batch'])

    def test_fabricated_proof_cannot_complete_tool_requirement(self):
        self.loop.accept_plan(plan(1,requires_tools=True))
        forged=review(['satisfied']);forged['checks'][0]['evidence_ids']=['missing:E0001']
        forged['checks'][0]['tool_call_ids']=['T99']
        self.assertEqual(self.loop.accept_review(forged,self.evidence,'声称找到证据')['decision'],'continue')
        self.evidence.items[E1]={'evidence_id':E1,'claim_type':'actual','value':{'value':123}}
        self.assertEqual(self.loop.accept_review(review(['satisfied'],proof=True),self.evidence,'有据结论')['decision'],'complete')

    def test_error_receipt_is_not_evidence_of_completion(self):
        self.loop.accept_plan(plan(1,requires_tools=True))
        self.loop.record_tool('finance_market_screen',{},'FinanceDB error: timeout',error=True)
        result=review(['satisfied']);result['checks'][0]['tool_call_ids']=['T1']
        self.assertEqual(self.loop.accept_review(result,self.evidence,'完成')['decision'],'continue')
        result=review(['blocked'],decision='blocked');result['checks'][0]['tool_call_ids']=['T1']
        self.assertEqual(self.loop.accept_review(result,self.evidence,'行情查询超时，尚未取得走势')['decision'],'blocked')

    def test_unattempted_available_lookup_cannot_be_waived_as_blocked(self):
        self.loop.accept_plan(plan(1,requires_tools=True))
        self.assertEqual(self.loop.accept_review(review(['blocked'],decision='blocked'),self.evidence,'没数据')['decision'],'continue')

    def test_omitted_original_ask_becomes_pending_work(self):
        result=review(['satisfied','satisfied']);result['missing_requirements']=[{'description':'用户还要求比较日期','acceptance':'明确两个来源日期'}]
        self.assertEqual(self.loop.accept_review(result,self.evidence,'结论')['decision'],'continue')
        self.assertEqual(self.loop.requirements[-1]['description'],'用户还要求比较日期')

    def test_single_answer_holds_partial_draft(self):
        self.loop.accept_plan(plan(mode='single'))
        self.assertFalse(self.loop.accept_review(review(['satisfied','missing']),self.evidence,'部分结论')['publish'])
        result=self.loop.accept_review(review(['satisfied','satisfied']),self.evidence,'完整结论')
        self.assertTrue(result['publish']);self.assertEqual(result['decision'],'complete')
        self.assertFalse(self.loop.followup({},self.evidence,self.control))

    def test_reviews_include_actual_answers_and_observations(self):
        self.loop.record_answer('用户看到的结论','有引用的原始结论')
        self.loop.record_tool('finance_market_screen',{'stock_codes':['600000']},'{"meta":{"status":"ok"},"rows":[]}')
        payload=json.loads(self.loop.review_messages(self.evidence,'新结论')[1]['content'])
        self.assertEqual(payload['delivered_answers'],['有引用的原始结论'])
        self.assertEqual(payload['candidate_answer'],'新结论')
        self.assertEqual(payload['tool_receipts'][0]['id'],'T1')

    def test_non_finance_tool_outputs_need_not_use_finance_envelopes(self):
        for result in ('["document one", "document two"]','42','"done"','{"meta":"description"}'):
            self.assertEqual(self.evidence.add_tool_result('read_documents',result),0)
            self.loop.record_tool('read_documents',{},result)
        self.assertEqual(len(self.loop.receipts),4)

    def test_clock_or_narrow_request_can_finish_once_without_extra_work(self):
        self.loop.accept_plan(plan(1,mode='single'))
        self.loop.accept_review(review(['satisfied']),self.evidence,'简答')
        self.assertFalse(self.loop.followup({},self.evidence,self.control))

    def test_budget_counts_planning_and_reviews_and_stops_no_progress(self):
        self.loop.auxiliary_calls=2
        self.loop.check_budget(self.loop.max_model_calls-2,self.control)
        self.assertTrue(self.control.force_final)
        fresh=GoalLoop(CONFIG,'任何问题');control=ToolLoopController()
        for _ in range(3):fresh.observe_round(control)
        self.assertEqual(control.stop_reason,'no_new_queries')
        fresh.started-=fresh.max_seconds+1;fresh.check_budget(0,control)
        self.assertEqual(fresh.stop_reason,'elapsed_budget')

    def test_duplicates_background_and_stage_limit(self):
        self.assertTrue(self.loop.record_answer('同一答复'))
        self.assertFalse(self.loop.record_answer(' 同一答复 '))
        self.assertFalse(self.loop.record_answer('同一答复'))
        self.assertFalse(self.loop.followup({},self.evidence,self.control))
        background=GoalLoop(CONFIG,'比较结果',background=True)
        self.assertFalse(background.enabled)
        self.assertFalse(background.followup({},self.evidence,self.control))


class GoalWorkerTests(unittest.TestCase):
    def run_worker(self,responses,*,protocol='json',stop_after_first=False,question='把证据和解释都说清楚',tool_effect=None,config=None):
        agent=MMClaw.__new__(MMClaw);agent.config={**CONFIG,'tool_calling_mode':protocol,**(config or {})}
        agent.memory=StatelessMemory('prompt');agent.debug=False;agent.use_stateless_arg_connector=True
        agent._stop_event=threading.Event();agent._activate_task_context=mock.Mock();agent._trace=mock.Mock()
        agent._guard_finance_answer=mock.Mock(side_effect=lambda draft,*args:draft)
        agent.connector=mock.Mock();agent.engine=mock.Mock(supports_native_tools=protocol=='native')
        agent.engine.tool_result_messages=lambda calls,results:[{'role':'tool','tool_call_id':c['id'],'content':r} for c,r in zip(calls,results)]
        agent._ask_with_stop=mock.Mock(side_effect=responses)
        agent._execute_tool_call=mock.Mock(return_value=('{"meta":{"status":"ok"},"rows":[],"evidence":[]}',False))
        if tool_effect is not None:agent._execute_tool_call.side_effect=tool_effect
        if stop_after_first:agent.connector.send.side_effect=lambda _:agent._stop_event.set()
        q=queue.Queue();q.put(question);q.put(None)
        with mock.patch('mmclaw.config.ConfigManager.get_full_prompt',return_value='prompt'),mock.patch('mmclaw.kernel.persist_model_call'), \
             mock.patch.object(EvidenceStore,'persist'),contextlib.redirect_stdout(io.StringIO()):agent._worker(q,'chat')
        return agent

    def test_completion_reviewer_reopens_premature_final_and_finishes_second_requirement(self):
        agent=self.run_worker([response(plan()),response({'tools':[],'content':'第一项结果','task_status':'complete'}),
            response(review(['satisfied','missing'])),
            response({'tools':[{'name':'finance_market_series_batch','args':{'stock_codes':['600000']}}],'content':''}),
            response({'tools':[],'content':'第二项结果','task_status':'complete'}),response(review(['satisfied','satisfied']))])
        self.assertEqual([x.args[0] for x in agent.connector.send.call_args_list],['第一项结果','第二项结果'])
        self.assertEqual(agent._execute_tool_call.call_count,1)
        self.assertEqual(agent._ask_with_stop.call_count,6)
        events=[c for c in agent._trace.call_args_list if c.args[0]=='turn_end']
        self.assertEqual(events[-1].kwargs['completion_status'],'complete')
        self.assertEqual(events[-1].kwargs['requirements_satisfied'],2)

    def test_selection_only_round_gets_new_schema_without_answer_or_review(self):
        agent=self.run_worker([response(plan(1,mode='single')),response({'tools':[],'content':'','select_tools':['finance_graph_analysis'],'task_status':'continue'}),
            response({'tools':[{'name':'finance_graph_analysis','args':{'stock_codes':['600000']}}],'content':''}),
            response({'tools':[],'content':'查证后的关联说明','task_status':'complete'}),response(review(['satisfied']))])
        self.assertEqual(agent._execute_tool_call.call_args.args[0],'finance_graph_analysis')
        self.assertEqual(agent._ask_with_stop.call_count,5)
        self.assertEqual(agent.connector.send.call_count,1)

    def test_bad_argument_is_returned_to_model_for_correction(self):
        agent=self.run_worker([response(plan(1)),
            response({'tools':[{'name':'finance_resolve_entities','args':{'query':'示例','entity_type':'stock'}}]}),
            response({'tools':[{'name':'finance_resolve_entities','args':{'query':'示例'}}]}),
            response({'content':'纠正后得到结论','task_status':'complete'}),response(review(['satisfied']))])
        self.assertEqual(agent._execute_tool_call.call_count,1)
        self.assertNotIn('entity_type',agent._execute_tool_call.call_args.args[1])
        self.assertEqual(agent.connector.send.call_args.args[0],'纠正后得到结论')

    def test_one_tool_exception_can_replan_without_aborting_turn(self):
        agent=self.run_worker([response(plan(1)),
            response({'tools':[{'name':'finance_resolve_entities','args':{'query':'示例'}}]}),
            response({'tools':[{'name':'finance_market_series_batch','args':{'stock_codes':['600000']}}]}),
            response({'content':'通过替代查询得到结论','task_status':'complete'}),response(review(['satisfied']))],
            tool_effect=[TypeError('invalid adapter state'),('{"meta":{"status":"ok"},"evidence":[]}',False)])
        self.assertEqual(agent._execute_tool_call.call_count,2)
        self.assertEqual(agent.connector.send.call_args.args[0],'通过替代查询得到结论')
        self.assertFalse(any(c.args[0]=='turn_error' for c in agent._trace.call_args_list))

    def test_tool_wrapper_preserves_user_stop(self):
        agent=MMClaw.__new__(MMClaw);agent._execute_tool_call=mock.Mock(side_effect=StopRequested)
        loop=GoalLoop(CONFIG,'查询');loop.bind_tools(get_native_tool_schemas(CONFIG,route=False))
        with self.assertRaises(StopRequested):agent._execute_goal_tool('finance_market_screen',{},True,False,loop)

    def test_claim_verification_uses_server_owned_turn_id(self):
        agent=self.run_worker([response(plan(1)),
            response({'tools':[{'name':'finance_claim_verify','args':{'turn_id':'wrong-date','evidence_ids':[E1]}}]}),
            response({'content':'验证结论','task_status':'complete'}),response(review(['satisfied']))])
        args=agent._execute_tool_call.call_args.args[1]
        self.assertRegex(args['turn_id'],r'^[a-f0-9]{32}$')

    def test_stop_after_stage_prevents_followup_calls(self):
        agent=self.run_worker([response(plan()),response({'tools':[],'content':'第一项结果','task_status':'continue'}),
                               response(review(['satisfied','missing']))],stop_after_first=True)
        self.assertEqual(agent._ask_with_stop.call_count,3)
        self.assertEqual(agent.connector.send.call_count,1)
        endings=[c.kwargs for c in agent._trace.call_args_list if c.args[0]=='turn_end']
        self.assertEqual(endings[-1]['completion_status'],'cancelled')

    def test_native_execution_obeys_same_plan_and_review(self):
        agent=self.run_worker([response(plan(1)),{'content':'','tool_calls':[{'id':'call1','name':'finance_market_screen','args':{}}]},
                               response({'content':'核对后的结论','task_status':'complete'}),response(review(['satisfied']))],protocol='native')
        self.assertEqual(agent._execute_tool_call.call_count,1)
        self.assertEqual([x.args[0] for x in agent.connector.send.call_args_list],['核对后的结论'])

    def test_simple_unrelated_task_does_not_force_financial_queries(self):
        p=plan(1,mode='single');p['selected_tools']=[];p['steps'][0]['tools']=[]
        agent=self.run_worker([response(p),response({'tools':[],'content':'清晰的改写','task_status':'complete'}),response(review(['satisfied']))],question='把这句话改得清楚些')
        self.assertEqual(agent._execute_tool_call.call_count,0)
        self.assertEqual(agent.connector.send.call_count,1)

    def test_generic_profile_can_plan_read_and_complete_non_financial_work(self):
        p=plan(1,mode='single',requires_tools=True)
        p.update(goal='概括给定文件',constraints=['只读'],selected_tools=['file_read'])
        p['requirements'][0].update(description='读取并概括文件',acceptance='给出与文件对应的摘要')
        p['steps'][0].update(action='读取文件后概括',tools=['file_read'])
        r=review(['satisfied']);r['checks'][0]['tool_call_ids']=['T1']
        agent=self.run_worker([response(p),response({'tools':[{'name':'file_read','args':{'path':'/tmp/example.txt'}}]}),
            response({'content':'文件的交接步骤是先保存配置，再验收变更。','task_status':'complete'}),response(r)],
            config={'tool_profile':'default','agentic_loop':{'enabled':True}},
            tool_effect=[('["先保存配置", "再验收变更"]',False)],question='读取给定文件并概括交接步骤')
        self.assertEqual(agent._execute_tool_call.call_args.args[0],'file_read')
        endings=[c.kwargs for c in agent._trace.call_args_list if c.args[0]=='turn_end']
        self.assertEqual(endings[-1]['completion_status'],'complete')
        self.assertEqual(agent.connector.send.call_count,1)

    def test_incomplete_single_answer_is_revised_before_publication(self):
        agent=self.run_worker([response(plan(mode='single')),response({'tools':[],'content':'遗漏了后半部分','task_status':'complete'}),
                               response(review(['satisfied','missing'])),response({'tools':[],'content':'完整覆盖两项要求','task_status':'complete'}),
                               response(review(['satisfied','satisfied']))])
        self.assertEqual([x.args[0] for x in agent.connector.send.call_args_list],['完整覆盖两项要求'])


if __name__=='__main__':unittest.main()
