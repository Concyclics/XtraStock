"""The bounded daily prompt must retain dynamic candidates and original pool."""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/pipeline'))
from daily_llm_payload import compact_v3_snapshot
import daily_ai_hardware_report_v3 as daily


class CandidatePayloadTests(unittest.TestCase):
    def test_failed_scenario_repetition_does_not_displace_current_validation(self):
        validation={'horizon_trading_days':20,'average_error_percentage_points':10.335,
                    'observed_interval_coverage_percent':77.56,'research_validation_passed':False,
                    'failed_requirements':['跨期表现不稳定'],'paired_error_difference_percentage_points':{'lower_95':-.468,'upper_95':.282}}
        scenario={'prediction_eligible':False,'horizon_trading_days':20,'status':'research_only',
                  'blockers':['校准未通过'],'reference_price':100,'expected_net_return':None,
                  'reference_assumption':'重复解释'*200,'model_id':'private-id'}
        source={'evidence':[{'evidence_id':'CAND0001','metric_id':'candidate_pool','value':{
                'component':'candidate_pool_summary','forecast_validation':[validation]}},
                {'evidence_id':'POOL0001','metric_id':'frozen_research_watchlist','value':{
                 'stock_code':'600036','research':{'scenarios':[scenario,{**scenario,'prediction_eligible':True,'expected_net_return':.1}]}}}]}
        result=compact_v3_snapshot(source)
        self.assertEqual(result['evidence'][0]['value']['forecast_validation'],[validation])
        failed,available=result['evidence'][1]['value']['research']['scenarios']
        self.assertEqual(failed['blockers'],['校准未通过']);self.assertFalse(failed['prediction_eligible'])
        self.assertNotIn('reference_assumption',failed);self.assertNotIn('model_id',available)
        self.assertEqual(available['expected_net_return'],.1)
        self.assertIn('reference_price',scenario)

    def test_candidate_citations_render_as_reader_sources_without_internal_ids(self):
        from mmclaw.presentation import render_finance_text
        source={'evidence':[{'evidence_id':'CAND0002','metric_id':'candidate_pool',
                            'claim_type':'interpretation','as_of_date':'2026-10-14',
                            'value':{'name':'示例公司','component':'candidate_pool'}}]}
        text=render_finance_text('长期论点继续跟踪。[evidence: CAND0002]',evidence=source)
        self.assertNotIn('CAND0002',text);self.assertNotIn('evidence:',text)
        self.assertIn('自动候选池与周期复核记录',text);self.assertIn('示例公司',text)

    def test_readable_half_year_and_decline_keep_source_period_and_direction(self):
        snapshot={'report_date':'2026-10-14','decision_cutoff':'2026-10-14T20:00:00+08:00',
                  'market_date':'2026-10-13','hard_gate_passed':False,
                  'evidence':[{'evidence_id':'FACT1','claim_type':'actual','quality_status':'verified',
                               'value':{'period_end':'2026-06-30','net_profit_parent_yoy':-17.88}}]}
        def check(text):
            return daily.validate_claims([{'claim_id':'C1','section_name':'长期候选池更新',
                'claim_type':'interpretation','claim_text':text,'evidence_ids':['FACT1']}],snapshot)
        claims,errors=check('2026年上半年归母净利润同比下降17.88%。')
        self.assertFalse(errors);self.assertEqual(claims[0]['validation_status'],'verified')
        self.assertTrue(check('2025年上半年归母净利润同比下降17.88%。')[1])
        self.assertTrue(check('2026年上半年归母净利润同比增长17.88%。')[1])
        self.assertNotIn('2026-Q2',daily.date_tokens(snapshot['evidence']))
        self.assertEqual(daily.date_tokens('项目计划2027Q2投产'),daily.date_tokens('项目计划2027年第二季度投产'))

    def test_size_pressure_keeps_summary_two_candidates_and_all_watchlist_decisions(self):
        evidence=[{'evidence_id':'CAND0001','metric_id':'candidate_pool',
                   'value':{'component':'candidate_pool_summary','state_counts':{'core':20}}}]
        evidence += [{'evidence_id':f'CAND{i:04}','metric_id':'candidate_pool',
                      'value':{'component':'candidate_pool','stock_code':f'{i:06}',
                               'financial_evidence':[{'value':40.91,'unit':'%'}],
                               'thesis':['x'*1100]*4}} for i in range(2,10)]
        evidence += [{'evidence_id':f'POOL{i:04}','metric_id':'frozen_research_watchlist',
                      'value':{'stock_code':f'{600000+i}','reason':'kept',
                               'research':{'stock_name':'example','watchlist_membership':{'reason':'duplicate'*500}}}}
                     for i in range(10)]
        evidence += [{'evidence_id':f'DOC{i:04}','metric_id':'published_document',
                      'value':{'parts':['y'*1100]*5}} for i in range(30)]
        source={'evidence':evidence};before=copy.deepcopy(source)
        result=compact_v3_snapshot(source);ids={e['evidence_id'] for e in result['evidence']}
        self.assertTrue({'CAND0001','CAND0002','CAND0003'}<=ids)
        self.assertTrue({f'POOL{i:04}' for i in range(10)}<=ids)
        self.assertEqual(source,before)
        pool=[e for e in result['evidence'] if e['metric_id']=='frozen_research_watchlist']
        self.assertTrue(all('watchlist_membership' not in e['value']['research'] for e in pool))
        self.assertLess(len(json.dumps(result,ensure_ascii=False)),91000)


if __name__=='__main__':unittest.main()
