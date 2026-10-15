"""Remove empty liability clauses while preserving the information readers need."""
import unittest
from unittest import mock

from mmclaw.reader_language import strip_boilerplate, READER_STYLE_PROMPT
from mmclaw.presentation import render_finance_text
from mmclaw.evidence import EvidenceStore
from mmclaw.config import ConfigManager
from mmclaw.goal_examples import EXAMPLES
from mmclaw.agentic_loop import GoalLoop, RESEARCH_SCOPE_PROMPT


class ReaderLanguageTests(unittest.TestCase):
    def test_empty_clauses_removed_at_each_position(self):
        for text in ('不构成投资建议。营收增长12.35%。',
                     '营收增长12.35%。不构成投资建议。',
                     '营收增长12.35%，不构成投资建议。',
                     '营收增长12.35%（不构成投资建议）。'):
            with self.subTest(text=text):
                self.assertEqual(strip_boilerplate(text), '营收增长12.35%。')

    def test_generic_legal_and_professional_identity_lines_removed(self):
        for text in ('以上仅供研究观察，不构成买卖建议。', '仅供参考。',
                     '- 投资有风险，入市需谨慎。', '盈亏自负。',
                     '我不是专业投资顾问。', '建议咨询专业投资顾问。',
                     'This is not financial advice.', 'For informational purposes only.'):
            with self.subTest(text=text):self.assertEqual(strip_boilerplate(text), '')

    def test_specific_limits_and_actual_business_risks_are_unchanged(self):
        for text in ('缺少2026-10-08行情，无法计算完整区间收益。',
                     '估值数据日期为2026-09-18，行情日期为2026-10-12。',
                     '模型MAE为3.444个百分点，未胜过零收益基线。',
                     '公告披露的是产能计划，订单金额未披露。',
                     '客户集中度较高；单一客户占营收60%，存在订单波动风险。',
                     '图谱关联不能证明订单或收入已兑现。',
                     '无法给出目标价，因为缺少盈利预测和估值输入。',
                     '这不构成订单确认。', '收入-12.35%，金额1,234.56元。'):
            with self.subTest(text=text):self.assertEqual(strip_boilerplate(text),text)

    def test_quoted_sources_titles_code_and_links_preserved(self):
        for text in ('公告写明“不构成投资建议”。', '《不构成投资建议》是一篇标题。',
                     '> 不构成投资建议。', '`不构成投资建议`',
                     '```text\n不构成投资建议。\n```',
                     '[原文](https://example.test/不构成投资建议?x=12.35)'):
            with self.subTest(text=text):self.assertEqual(strip_boilerplate(text),text)

    def test_material_clause_after_disclaimer_is_not_removed(self):
        self.assertEqual(strip_boilerplate('不构成投资建议，但该公司现金流下降。'), '但该公司现金流下降。')
        self.assertEqual(strip_boilerplate('仅供参考，数据缺少三天行情。'), '数据缺少三天行情。')

    def test_citation_on_same_sentence_survives(self):
        text='营收12亿元，不构成投资建议[来源1]。'
        self.assertEqual(strip_boilerplate(text),'营收12亿元[来源1]。')

    def test_clause_removal_is_idempotent(self):
        original='- 营收12亿元，不构成投资建议。\n\n- 仅供参考。\n\n价格截至2026-10-12。'
        result=strip_boilerplate(original)
        self.assertEqual(strip_boilerplate(result),result)
        self.assertIn('价格截至2026-10-12',result)

    def test_renderer_does_not_reintroduce_source_disclaimer(self):
        eid='a'*32+':E0001'
        result=render_finance_text('动量偏弱。['+eid+'] 不构成投资建议。',[
            {'evidence_id':eid,'claim_type':'model_signal','as_of_date':'2026-10-13','value':{}}])
        self.assertIn('量化研究记录',result)
        self.assertNotIn('仅供研究',result)
        self.assertNotIn('不构成投资建议',result)
        self.assertIn('[来源1]',result)

    def test_disclaimer_alone_cannot_pass_as_a_completed_answer(self):
        store=EvidenceStore('a'*32,'分析模型结果')
        result=store.validate('仅供参考，不构成投资建议。',require_date=False)
        self.assertFalse(result['passed'])
        self.assertIn('boilerplate_only_answer',{v['code'] for v in result['violations']})

    def test_same_policy_in_actual_chat_and_repair_prompts(self):
        with mock.patch.object(ConfigManager,'_get_engine_context',return_value='') if hasattr(ConfigManager,'_get_engine_context') else mock.patch.dict('os.environ',{}):
            prompt=ConfigManager.get_full_prompt({'tool_profile':'finance_chat_readonly','tool_calling_mode':'json'})
        self.assertIn(READER_STYLE_PROMPT,prompt)
        store=EvidenceStore('a'*32,'分析模型结果')
        self.assertIn(READER_STYLE_PROMPT,store.repair_prompt('草稿',{'violations':[]}))

    def test_factor_example_models_analysis_not_a_disclaimer_ending(self):
        import json
        sample=next(x for x in EXAMPLES if x['id']=='DEMO_FACTOR')
        text=json.dumps(sample,ensure_ascii=False,default=list)
        self.assertNotIn('仅供研究观察',text)
        self.assertIn('价格回落',text)

    def test_planning_and_review_share_reader_scope_without_weakening_evidence(self):
        loop=GoalLoop({'tool_profile':'finance_chat_readonly'},'解释可用研究信号')
        store=EvidenceStore('a'*32,loop.user_text)
        for messages in (loop.plan_messages([]),loop.review_messages(store,'有来源的描述性分析')):
            self.assertIn(RESEARCH_SCOPE_PROMPT,messages[0]['content'])
            self.assertIn(READER_STYLE_PROMPT,messages[0]['content'])
        # This policy does not accept an unsupported result or leak into unrelated tasks.
        self.assertFalse(store.validate('营收增长35.2%。',require_date=False)['passed'])
        generic=GoalLoop({'agentic_loop':{'enabled':True}},'整理我提供的文字')
        self.assertNotIn(RESEARCH_SCOPE_PROMPT,generic.plan_messages([])[0]['content'])


if __name__=='__main__':unittest.main()
