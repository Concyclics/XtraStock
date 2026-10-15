import unittest
from unittest import mock
from MMClaw.tests import test_goal_loop as fixtures
plan, review, response = fixtures.plan, fixtures.review, fixtures.response


class DeliveryContractTests(unittest.TestCase):
    setUp = fixtures.GoalStateTests.setUp
    def test_missing_deliverables_never_publish_even_when_reviewer_requests_it(self):
        item=review(['missing','missing'])
        self.assertFalse(self.loop.accept_review(item,self.evidence,'资料日期。前述情况仍不确定。')['publish'])

    def test_order_is_checked_within_and_between_stages(self):
        p=plan();p['requirements'][1]['delivery_after']=['R1'];self.loop.accept_plan(p)
        item=review(['missing','satisfied'])
        self.assertFalse(self.loop.accept_review(item,self.evidence,'先说公司')['publish'])
        item=review(['satisfied','satisfied']);item['candidate_delivery']['requirement_ids']=['R2','R1']
        self.assertFalse(self.loop.accept_review(item,self.evidence,'倒置顺序')['publish'])
        item=review(['satisfied','missing']);self.assertTrue(self.loop.accept_review(item,self.evidence,'完整概览')['publish'])
        self.loop.record_answer('完整概览')
        item=review(['satisfied','satisfied']);item['candidate_delivery']['requirement_ids']=['R2']
        self.assertTrue(self.loop.accept_review(item,self.evidence,'公司跟进')['publish'])
        self.loop.record_answer('公司跟进');self.assertEqual(self.loop.delivered_requirement_ids,{'R1','R2'})

    def test_self_containment_and_new_progress_are_required(self):
        item=review(['satisfied','satisfied']);item['candidate_delivery']['self_contained']=False
        self.assertFalse(self.loop.accept_review(item,self.evidence,'前文数据如此')['publish'])
        self.loop.accept_review(review(['satisfied','missing']),self.evidence,'首项');self.loop.record_answer('首项')
        result=self.loop.accept_review(review(['satisfied','missing']),self.evidence,'换个说法重复首项')
        self.assertFalse(result['publish'])

    def test_only_exact_reviewed_candidate_is_authorized(self):
        self.loop.accept_review(review(['satisfied','satisfied']),self.evidence,'通过的完整答案')
        self.assertTrue(self.loop.delivery_allowed('通过的完整答案'))
        self.assertFalse(self.loop.delivery_allowed('未审核的新草稿'))
        self.loop.review_failed();self.assertFalse(self.loop.delivery_allowed('通过的完整答案'))

    def test_repeated_candidate_cannot_falsely_finish_a_new_requirement(self):
        self.loop.accept_review(review(['satisfied','missing']),self.evidence,'已经发送的结果')
        self.loop.record_answer('已经发送的结果[来源1]', '已经发送的结果')
        item=review(['satisfied','satisfied']);item['candidate_delivery']['requirement_ids']=['R2']
        result=self.loop.accept_review(item,self.evidence,' 已经发送的结果 ')
        self.assertFalse(result['publish']);self.assertEqual(result['decision'],'continue')
        self.assertEqual(self.loop.delivered_requirement_ids,{'R1'})

    def test_catalog_includes_arguments_before_dynamic_tool_selection(self):
        item=next(r for r in self.loop.catalog() if r['name']=='finance_graph_analysis')
        self.assertIn('stock_codes',item['arguments']);self.assertNotIn('entity_id',item['arguments'])

    def test_final_review_cannot_drop_executor_continuation(self):
        self.loop.update({'task_status':'continue','next_step':'核对已承诺的公司细节'})
        result=self.loop.accept_review(review(['satisfied','satisfied']),self.evidence,'概览。下面继续核对细节。')
        self.assertFalse(result['publish']);self.assertEqual(result['decision'],'continue')
        self.loop.update({'task_status':'complete'})
        self.assertTrue(self.loop.accept_review(review(['satisfied','satisfied']),self.evidence,'完成的整体分析')['publish'])

    def test_delivery_dependencies_cannot_cycle(self):
        p=plan();p['requirements'][0]['delivery_after']=['R2']
        with self.assertRaises(ValueError):self.loop.accept_plan(p)


class RuntimeDeliveryTests(unittest.TestCase):
    run_worker = fixtures.GoalWorkerTests.run_worker
    def test_review_limit_never_sends_rejected_fragment(self):
        fragment='资料日期。上述数据意味着不能确认。'
        agent=self.run_worker([response(plan()),response({'content':fragment,'tools':[],'task_status':'complete'}),
            response(review(['missing','missing']))], config={'agentic_loop':{'max_reviews':1}})
        sent=[c.args[0] for c in agent.connector.send.call_args_list]
        self.assertFalse(any(fragment in m for m in sent))
        self.assertEqual(len(sent),1)
        presentations=[c for c in agent._trace.call_args_list if c.args[0]=='answer_presentation']
        self.assertEqual(presentations,[])
