"""Daily generation, repair and rendering use the same non-boilerplate contract."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/pipeline'))
import daily_ai_hardware_report_v3 as daily
import daily_ai_hardware_codex_agent as legacy
from user_presentation import READER_STYLE_PROMPT


class DailyReaderLanguageTests(unittest.TestCase):
    def snapshot(self):
        return {'report_date':dt.date(2026,10,14),'decision_cutoff':dt.datetime(2026,10,14,0,50,tzinfo=dt.timezone.utc),
                'market_date':dt.date(2026,10,13),'hard_gate_passed':False,
                'evidence':[{'evidence_id':'DOC0001','claim_type':'document_claim',
                'value':{'title':'公司发布项目公告'},'quality_status':'verified'}]}

    def test_generation_prompt_omits_unrequested_prediction_placeholders(self):
        prompt=daily.build_prompt(Path('snapshot.json'),Path('claims.json'))
        self.assertIn(READER_STYLE_PROMPT,prompt)
        self.assertIn('省略数值预测栏目',prompt)
        self.assertNotIn('必须写暂不提供',prompt)
        self.assertNotIn('shadow 写“仅供研究观察”',prompt)

    def test_repair_prompt_keeps_same_style_without_loosening_evidence(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(daily,'openai_compatible_complete',return_value='{"claims":[]}') as call:
            daily.repair_openai_compatible_claims(Path(d),self.snapshot(),[],['unsupported numeric'],
                'test-model',20,'https://provider.invalid','TEST_KEY','high')
            messages=call.call_args.args[0]
            self.assertIn(READER_STYLE_PROMPT,messages[1]['content'])
            self.assertIn('不得新增证据中没有的数字',messages[1]['content'])

    def test_disclaimer_with_citation_is_not_a_report_claim(self):
        rows,errors=daily.validate_claims([{'claim_id':'C001','section_name':'今日核心判断',
            'claim_type':'interpretation','claim_text':'仅供参考，不构成投资建议。',
            'evidence_ids':['DOC0001']}],self.snapshot())
        self.assertEqual(rows[0]['validation_status'],'rejected')
        self.assertTrue(any('boilerplate_only' in e for e in errors))

    def test_missing_prediction_approval_still_rejects_numeric_forecast(self):
        rows,errors=daily.validate_claims([{'claim_id':'C001','section_name':'今日核心判断',
            'claim_type':'interpretation','claim_text':'目标价100元，预期收益20%。',
            'evidence_ids':['DOC0001']}],self.snapshot())
        self.assertEqual(rows[0]['validation_status'],'rejected')
        self.assertTrue(any('numeric_prediction_without_approved_scenario' in e for e in errors))

    def test_legacy_failure_is_one_operational_notice_without_empty_sections(self):
        text=legacy.deterministic_degraded_report(dt.date(2026,10,14),dt.date(2026,10,13),
            dt.datetime.now(dt.timezone.utc),'TimeoutError test')
        self.assertEqual(text.count('\n## '),1)
        self.assertNotIn('今日投资建议',text)
        self.assertIn('模型生成降级原因',text)


if __name__=='__main__':unittest.main()
