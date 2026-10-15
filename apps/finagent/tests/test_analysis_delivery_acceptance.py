import copy
import unittest
from scripts.agent.replay_agentic_loop import delivery_checks,graph_capability_check

class DeliveryAcceptanceTests(unittest.TestCase):
    def test_old_false_pass_with_all_missing_is_rejected(self):
        trace=[{'event':'goal_review','requirements':[{'id':'R1','status':'missing'},{'id':'R2','status':'missing'}],
                'review':{'publish':True}}, {'event':'answer_presentation','presented_answer':'资料日期。前述材料……'}]
        self.assertFalse(delivery_checks(trace)['passed'])

    def test_order_and_progress_in_real_deliveries(self):
        requirements=[{'id':'R1','status':'satisfied','delivery_after':[]},
                      {'id':'R2','status':'satisfied','delivery_after':['R1']}]
        trace=[{'event':'goal_review','requirements':requirements,'review':{'publish':True}},
               {'event':'answer_presentation','delivery':{'requirement_ids':['R1','R2'],'self_contained':True}}]
        self.assertTrue(delivery_checks(trace)['passed'])
        trace[-1]['delivery']['requirement_ids']=['R2','R1'];self.assertFalse(delivery_checks(trace)['passed'])

    def test_promise_to_continue_cannot_be_terminal_success(self):
        trace=[{'event':'research_stage','completion_status':'complete','task_status':'continue'}]
        self.assertFalse(delivery_checks(trace)['passed'])

    def test_calling_empty_graph_is_not_capability_success(self):
        self.assertFalse(graph_capability_check([{'tool':'finance_graph_analysis','result':{'meta':{'status':'unavailable'},'rows':[]}}])['passed'])
        row={'review_status':'classified','source_document_id':'d','evidence_text':'source','claim_class':'extracted_claim'}
        observation={'tool':'finance_graph_analysis','result':{'meta':{'status':'ok'},'rows':[row]}}
        self.assertTrue(graph_capability_check([observation])['passed'])
        row['review_status']='unreviewed';self.assertFalse(graph_capability_check([observation])['passed'])
