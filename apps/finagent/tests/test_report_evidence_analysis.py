"""A local data gap must not erase supported research conclusions."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/pipeline'))
import daily_ai_hardware_report_v3 as daily
from report_capabilities import analysis_capabilities


class EvidenceAnalysisTests(unittest.TestCase):
    def snapshot(self):
        return dict(report_date=dt.date(2026,10,13),decision_cutoff='2026-10-13T08:00:00+08:00',
                    market_date=dt.date(2026,10,12),hard_gate_passed=False,
                    hard_gate_blockers=['failed:factor.daily_scores'],evidence=[{
                        'evidence_id':'DOC0001','claim_type':'document_claim',
                        'metric_id':'published_document','quality_status':'unverified',
                        'value':{'title':'公司发布新产品公告','source_name':'公告网站'}}])

    def args(self, **kw):
        return SimpleNamespace(**(dict(skip_model=False,provider='openai_compatible',
            model='gpt-6-astra',timeout=60,reasoning_effort='high',base_url='https://invalid.test',api_key_env='TEST_KEY') | kw))

    def claim(self, text='来源报道公司发布新产品，需求兑现情况值得后续核查。', **kw):
        return dict(claim_id='C1',section_name='今日核心判断',claim_type='interpretation',
                    claim_text=text,evidence_ids=['DOC0001'],metadata={'recommendation':False}) | kw

    def test_missing_factor_still_invokes_model_and_retains_analysis(self):
        snapshot=self.snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'claims.json';path.write_text(json.dumps({'claims':[self.claim()]}))
            with patch.object(daily,'run_openai_compatible',return_value=path) as provider:
                claims,status,errors,repair,mode=daily.generate_report_claims(snapshot,snapshot,Path(tmp),self.args())
        provider.assert_called_once()
        self.assertEqual(status,'degraded');self.assertEqual(mode,'model_evidence_analysis')
        self.assertFalse(errors);self.assertEqual(claims[0]['validation_status'],'verified')
        self.assertTrue(analysis_capabilities(snapshot)['evidence_analysis'])
        self.assertFalse(analysis_capabilities(snapshot)['stock_recommendations'])

    def test_provider_failure_retains_only_source_attributed_digest(self):
        snapshot=self.snapshot()
        with tempfile.TemporaryDirectory() as tmp, patch.object(daily,'run_openai_compatible',side_effect=TimeoutError('secret/internal/path')):
            claims,status,errors,_,mode=daily.generate_report_claims(snapshot,snapshot,Path(tmp),self.args())
        self.assertEqual(mode,'source_digest');self.assertEqual(status,'degraded')
        self.assertTrue(any('来源摘录' in c['claim_text'] for c in claims))
        self.assertNotIn('secret/internal/path',json.dumps(claims))
        self.assertIn('secret/internal/path',str(errors))
        self.assertTrue(all(c['validation_status']=='verified' for c in claims))

    def test_bad_repair_never_erases_valid_claim_or_overrides_rejected(self):
        snapshot=self.snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'claims.json';path.write_text(json.dumps({'claims':[
                self.claim(),self.claim('建议买入并加仓。',claim_id='C2')]}))
            with patch.object(daily,'run_openai_compatible',return_value=path),patch.object(daily,'repair_openai_compatible_claims',side_effect=TimeoutError('repair failed')):
                claims,status,errors,repair,mode=daily.generate_report_claims(snapshot,snapshot,Path(tmp),self.args())
        self.assertTrue(repair);self.assertEqual(status,'degraded')
        self.assertEqual([c['validation_status'] for c in claims],['verified','rejected'])
        self.assertNotIn('建议买入',daily.render_report(snapshot['report_date'],dt.datetime.now(dt.timezone.utc),snapshot['market_date'],claims,status,[]))

    def test_numerical_forecast_remains_forbidden_without_approved_scenario(self):
        claims,errors=daily.validate_claims([self.claim('目标价100元，预期收益20%。')],self.snapshot())
        self.assertEqual(claims[0]['validation_status'],'rejected')
        self.assertTrue(any('numeric_prediction_without_approved_scenario' in e for e in errors))

    def test_no_sources_cannot_be_marked_verified(self):
        snapshot=self.snapshot();snapshot['evidence']=[]
        with tempfile.TemporaryDirectory() as tmp,patch.object(daily,'run_openai_compatible') as provider:
            claims,status,_,_,_=daily.generate_report_claims(snapshot,snapshot,Path(tmp),self.args())
        provider.assert_not_called();self.assertEqual(claims,[]);self.assertEqual(status,'quarantined')

    def test_rejected_document_does_not_enable_analysis(self):
        snapshot=self.snapshot();snapshot['evidence'][0]['quality_status']='quarantined'
        self.assertFalse(analysis_capabilities(snapshot)['evidence_analysis'])


    def focus_snapshot(self):
        snapshot=self.snapshot();snapshot['shared_research']={'watchlist':[]}
        snapshot['evidence']=[{'evidence_id':'FOCUS0001','claim_type':'interpretation',
            'metric_id':'research_focus','quality_status':'unverified',
            'value':{'label':'测试公司','stock_codes':['000001'],'priority':32,
                     'evidence':[{'title':'公司披露新产品公告'}]}}]
        return snapshot

    def test_focus_cannot_become_buy_instruction_pool_or_revenue_growth(self):
        for text,section in [('买入000001。','今日核心判断'),
            ('建议重点配置测试公司。','今日核心判断'),
            ('测试公司列入个股观察池。','个股观察池'),
            ('测试公司营收增长32%。','今日核心判断')]:
            with self.subTest(text=text):
                claim=self.claim(text,section_name=section,evidence_ids=['FOCUS0001'])
                rows,errors=daily.validate_claims([claim],self.focus_snapshot())
                self.assertEqual(rows[0]['validation_status'],'rejected');self.assertTrue(errors)

    def test_bad_source_rejects_otherwise_valid_interpretation(self):
        snapshot=self.snapshot();snapshot['evidence'][0]['quality_status']='quarantined'
        rows,errors=daily.validate_claims([self.claim()],snapshot)
        self.assertEqual(rows[0]['validation_status'],'rejected');self.assertTrue(errors)

    def test_successful_but_invalid_repair_preserves_verified_original(self):
        snapshot=self.snapshot()
        with tempfile.TemporaryDirectory() as tmp:
            first=Path(tmp)/'first.json';repair=Path(tmp)/'repair.json'
            first.write_text(json.dumps({'claims':[self.claim(),self.claim('买入000001。',claim_id='C2')]}))
            repair.write_text(json.dumps({'claims':[self.claim('买入000001。',claim_id='C3')]}))
            with patch.object(daily,'run_openai_compatible',return_value=first),patch.object(daily,'repair_openai_compatible_claims',return_value=repair):
                rows,status,*_=daily.generate_report_claims(snapshot,snapshot,Path(tmp),self.args())
        self.assertEqual(status,'degraded')
        self.assertIn('C1',[r['claim_id'] for r in rows if r['validation_status']=='verified'])


if __name__=='__main__':unittest.main()
