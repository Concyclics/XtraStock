import json,unittest
from mmclaw.kernel import MMClaw
from mmclaw.evidence import EvidenceStore, _answer_numbers

class CompanyResearchCompletionTests(unittest.TestCase):
 def setUp(self):
  self.agent=object.__new__(MMClaw);self.agent.config={'tool_profile':'finance_chat_readonly'}
 def test_status_only_is_not_a_completed_company_analysis(self):
  store=EvidenceStore('a'*32,'分析长鑫科技688825的财务和图谱')
  store.add_tool_result('finance_data_status',json.dumps({'meta':{'status':'ok'},'evidence':[]}))
  self.assertTrue(self.agent._needs_company_research(store))
 def test_uuid_is_not_a_financial_number_but_real_amount_remains_checked(self):
  values=_answer_numbers('asset_id=30b513b3-c705-4f85-8682-fe3299c9ae56，价格88.55元')
  self.assertEqual(values,[(88.55,'88.55')])
 def test_real_failed_or_empty_bundle_does_not_retry_forever(self):
  store=EvidenceStore('a'*32,'分析688825')
  store.add_tool_result('finance_research_bundle',json.dumps({'meta':{'status':'blocked'}}))
  self.assertFalse(self.agent._needs_company_research(store))
 def test_real_evidence_and_clock_questions_are_not_forced_into_research(self):
  store=EvidenceStore('a'*32,'分析688825')
  store.items={'x':{'claim_type':'actual'}}
  self.assertFalse(self.agent._needs_company_research(store))
  self.assertFalse(self.agent._needs_company_research(EvidenceStore('a'*32,'现在北京时间几点')))
 def test_resolved_name_also_gets_substantive_lookup(self):
  store=EvidenceStore('a'*32,'分析长鑫科技')
  store.items={'x':{'claim_type':'data_quality','tool_name':'finance_resolve_entities'}}
  self.assertTrue(self.agent._needs_company_research(store))
if __name__=='__main__':unittest.main()
