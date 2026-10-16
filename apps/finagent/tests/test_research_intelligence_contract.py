import datetime as dt
import contextlib
import copy
import io
import json
import os
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'MMClaw'))
from mmclaw.finance_db_helper import research_analysis_status
from mmclaw.evidence import EvidenceStore
from scripts.analytics.research_intelligence import intelligence_rows, load_intelligence


class IntelligenceContractTests(unittest.TestCase):
    def test_experiment_page_keeps_all_rows_and_citations_with_shared_notes(self):
        from unittest.mock import MagicMock
        from mmclaw import finance_db_helper as helper
        from mmclaw.tools import FinanceDBTool
        cutoff=dt.datetime(2026,10,16,0,tzinfo=dt.timezone.utc)
        notes=[f'Rule {i}: '+('Preserve historical maturity, cohort, costs and partial-year boundaries. '*2) for i in range(20)]
        units={'net_return':'fraction','initial_cash':'CNY',**{f'other_{i}':'fraction' for i in range(30)}}
        rows=[{'component':'offline_experiment_result','project':'study','available_at':cutoff,
            'metrics':{'model':'candidate','year':str(2022+i),'net_return':i/100,'initial_cash':1000000},
            'metric_units':units,'methodology_notes':notes,'year_note':'Partial year' if i==4 else 'Full year',
            'training_status':'purged','prediction_eligible':False,'gate_status':'research_only'} for i in range(5)]
        state={'scope':'experiments','usage':'research','decision_cutoff':cutoff,
            'experiments':{'rows':rows,'catalog':{'projects':[{'project_key':'study',
                'result_contract':{'methodology_notes':notes,'metric_units':units}}]},
                'offset':0,'page_size':5,'next_offset':5}}
        original=copy.deepcopy(state)
        stream=io.StringIO()
        with patch('scripts.analytics.research_intelligence.load_intelligence',return_value=state),patch.object(
            helper,'connect',return_value=MagicMock()),patch.object(helper,'v3_request_context',return_value=('page',cutoff)),contextlib.redirect_stdout(stream):
            helper.run_v3_research_intelligence(str(ROOT),{})
        raw=stream.getvalue()
        bounded=FinanceDBTool._bound_child_output(raw) if len(raw)>FinanceDBTool.MAX_OUTPUT_CHARS else raw
        payload=json.loads(bounded)
        self.assertEqual(len(payload['rows']),5);self.assertEqual(len(payload['evidence']),5)
        self.assertFalse(payload['meta'].get('output_compacted',False))
        self.assertEqual(payload['meta']['experiment_next_offset'],5)
        self.assertEqual(payload['meta']['experiment_catalog']['projects'][0]['result_contract']['methodology_notes'],notes)
        for row,evidence in zip(payload['rows'],payload['evidence']):
            self.assertEqual(row,evidence['value'])
            self.assertEqual(row['metric_units'],{'net_return':'fraction','initial_cash':'CNY'})
            self.assertEqual(row['methodology_ref']['project'],'study')
            self.assertFalse(row['prediction_eligible'])
            self.assertIn('year_note',row)
        self.assertEqual(state,original)
        store=EvidenceStore('page','compare research')
        self.assertEqual(store.add_tool_result('finance_research_intelligence',bounded),5)
        self.assertFalse(store.tool_errors)

    def test_experiment_unique_or_missing_shared_methodology_stays_on_row(self):
        row={'project':'one','metrics':{'net_return':.1},'metric_units':{'net_return':'fraction'},
             'methodology_notes':['Unique source limitation']}
        for catalog in ({},{'projects':[{'project_key':'two','result_contract':{'methodology_notes':row['methodology_notes']}}]},
                        {'projects':[{'project_key':'one','result_contract':{'methodology_notes':['Different notes']}}]}):
            result=intelligence_rows({'experiments':{'rows':[row],'catalog':catalog}})
            self.assertEqual(result,[row])

    def test_pool_summary_survives_budget_and_compares_real_instants(self):
        state={'candidate_pool':{'status':'available','as_of_date':'2026-10-13',
            'decision_cutoff':'2026-10-14T19:00:00+08:00','rows':[{'stock_code':'600036',
            'as_of_date':'2026-10-14','available_at':'2026-10-14T20:00:00+08:00'}],
            'mining':{'as_of_date':'2026-10-14','updated_at':'2026-10-14T11:30:00+00:00'}}}
        rows=intelligence_rows(state)
        self.assertEqual(rows[0]['component'],'candidate_pool_summary')
        self.assertEqual(rows[0]['available_at'],dt.datetime(2026,10,14,11,30,tzinfo=dt.timezone.utc))
        self.assertEqual(rows[1]['as_of_date'],'2026-10-14')
        self.assertEqual(rows[1]['available_at'],'2026-10-14T20:00:00+08:00')

    def test_bad_recommendation_gate_does_not_discard_company_facts(self):
        state=research_analysis_status([{'stock_code':'000333'}],'fail')
        self.assertFalse(state['recommendation_allowed']);self.assertTrue(state['analysis_allowed'])
        store=EvidenceStore('test','只核查财务')
        result={'meta':state,'evidence':[{'evidence_id':'test:E0001','claim_type':'actual','value':{'revenue':3}}]}
        self.assertEqual(store.add_tool_result('finance_research_bundle',json.dumps(result)),1)
        self.assertEqual(store.tool_errors,[])

    def test_missing_data_stays_unavailable(self):
        self.assertEqual(research_analysis_status([],'fail')['status'],'unavailable')
        self.assertFalse(research_analysis_status([{}],'warn')['recommendation_allowed'])

    def test_internal_references_are_separate_from_citable_rows(self):
        rows=intelligence_rows({'focus':{'rows':[{'label':'新公告','references':{'revision_id':'private'}}]},
            'learning':{'update_status':'trained','research_scenarios':[{'stock_code':'600036','references':{'model_id':'private'}}]}})
        self.assertEqual([r['component'] for r in rows],['research_focus','learning_state','research_scenario'])
        self.assertNotIn('private',json.dumps(rows))

    def test_future_asof_rejected_and_cutoff_passed_to_both_sources(self):
        focus=types.ModuleType('scripts.analytics.research_focus')
        learning=types.ModuleType('scripts.analytics.learning_state')
        seen=[]
        def loader(conn,cutoff,asof,codes,limit):seen.append((cutoff,asof,codes,limit));return {'rows':[]}
        focus.load_focus=learning.load_learning_state=loader
        cutoff=dt.datetime(2026,10,13,15,tzinfo=dt.timezone.utc)
        with patch.dict(sys.modules,{'scripts.analytics.research_focus':focus,'scripts.analytics.learning_state':learning}):
            with self.assertRaisesRegex(ValueError,'exceed'):load_intelligence(None,cutoff,'2026-10-14')
            state=load_intelligence(None,cutoff,stock_codes=['600036','600036'],limit=100)
        self.assertEqual(len(seen),2);self.assertEqual(seen[0][2:],(['600036'],20))
        self.assertEqual(state['as_of_date'],dt.date(2026,10,13))

    def test_failed_learning_keeps_focus_and_sanitizes_failure(self):
        cutoff=dt.datetime.now(dt.timezone.utc)
        with patch('scripts.analytics.research_focus.load_focus',return_value={'status':'available','rows':[{'label':'有效公告'}]}),patch(
            'scripts.analytics.learning_state.load_learning_state',side_effect=RuntimeError('secret SQL/private path')):
            state=load_intelligence(None,cutoff)
        self.assertEqual([r['component'] for r in intelligence_rows(state)],['research_focus'])
        self.assertEqual(state['component_errors'][0]['component'],'learning')
        self.assertNotIn('private',json.dumps(state,default=str));self.assertNotIn('secret SQL',json.dumps(state,default=str))

    def test_failed_focus_keeps_learning(self):
        with patch('scripts.analytics.research_focus.load_focus',side_effect=TimeoutError('test failure')),patch(
            'scripts.analytics.learning_state.load_learning_state',return_value={'update_status':'complete','research_models':[]}):
            state=load_intelligence(None,dt.datetime.now(dt.timezone.utc))
        self.assertEqual(state['focus']['status'],'unavailable')
        self.assertEqual([r['component'] for r in intelligence_rows(state)],['learning_state'])

    def test_both_failed_have_no_citable_rows(self):
        with patch('scripts.analytics.research_focus.load_focus',side_effect=TimeoutError()),patch(
            'scripts.analytics.learning_state.load_learning_state',side_effect=TimeoutError()):
            state=load_intelligence(None,dt.datetime.now(dt.timezone.utc))
        self.assertEqual(intelligence_rows(state),[]);self.assertEqual(len(state['component_errors']),2)

    @unittest.skipUnless(os.getenv('FINAGENT_TEST_DB')=='1','requires local DB; read-only transaction')
    def test_database_error_is_rolled_back_before_next_source(self):
        from scripts.analytics.common import connect
        def fail(conn,*args):conn.execute('SELECT 1/0')
        def good(conn,*args):
            self.assertEqual(conn.execute('SELECT 1 AS n').fetchone()['n'],1)
            return {'update_status':'complete','research_models':[]}
        with connect(autocommit=False,attempts=1) as conn:
            conn.execute('SET TRANSACTION READ ONLY');conn.execute("SET LOCAL statement_timeout='3s'")
            with patch('scripts.analytics.research_focus.load_focus',side_effect=fail),patch(
                'scripts.analytics.learning_state.load_learning_state',side_effect=good):
                state=load_intelligence(conn,dt.datetime.now(dt.timezone.utc))
            self.assertEqual(state['component_errors'][0]['error_type'],'DivisionByZero')
            components=[r['component'] for r in intelligence_rows(state)]
            self.assertIn('learning_state',components)
            self.assertNotIn('research_focus',components)
            self.assertEqual([e['component'] for e in state['component_errors']],['focus'])
            conn.rollback()


    def test_tool_reports_actual_data_dates_and_component_failure(self):
        from unittest.mock import MagicMock
        from mmclaw import finance_db_helper as helper
        cutoff=dt.datetime(2026,10,13,16,5,tzinfo=dt.timezone.utc)
        state={'as_of_date':dt.date(2026,10,14),'decision_cutoff':cutoff,'scope':'all','usage':'research',
            'focus':{'status':'available','as_of_date':'2026-10-13','decision_cutoff':'2026-10-13T15:00:00Z',
                     'rows':[{'label':'公告跟踪'}]},
            'learning':{'update_status':'complete','as_of_market_date':'2026-10-12'},
            'component_errors':[{'component':'example','error_type':'TimeoutError'}]}
        with patch('scripts.analytics.research_intelligence.load_intelligence',return_value=state),patch.object(
            helper,'connect',return_value=MagicMock()),patch.object(helper,'v3_request_context',return_value=('test',cutoff)),patch.object(
            helper,'emit_v3') as emit:
            helper.run_v3_research_intelligence(str(ROOT),{})
        result=emit.call_args.kwargs
        self.assertEqual(result['data_through'],'2026-10-13')
        self.assertEqual([str(e['as_of_date']) for e in result['evidence']],['2026-10-13','2026-10-12'])
        self.assertEqual(result['extra_meta']['component_errors'],state['component_errors'])


    def test_monitor_projection_keeps_results_without_repeated_window_details(self):
        group={'stock_code':'600036','horizon_trading_days':5,'status':'review_required',
            'mature_observations':30,'signal_dates':25,'independent_windows':5,
            'review_required':True,'review_reasons':['误差需复核'],
            'recent':{'mae':.03,'observations':25,'readable':{'detail':'duplicate'*1000}},
            'prior':{'mae':.02,'observations':25},'changes':{'mae_change_percentage_points':1}}
        state={'learning':{'metric_units':{'mae':'return_ratio'},'prospective_monitor':{
            'mature_observations':30,'coverage':{'truncated':False},
            'by_horizon':[group],'by_stock':[group]}}}
        output=intelligence_rows(state)[0]
        monitor=output['prospective_monitor']
        self.assertEqual(monitor['by_stock'][0]['review_reasons'],['误差需复核'])
        self.assertEqual(monitor['by_horizon'][0]['recent']['mae'],.03)
        self.assertEqual(monitor['by_horizon'][0]['changes']['mae_change_percentage_points'],1)
        self.assertEqual(output['metric_units']['mae'],'return_ratio')
        self.assertNotIn('duplicate',json.dumps(output));self.assertIn('duplicate',json.dumps(state))


    def test_monitor_budget_keeps_review_first_and_discloses_omitted_groups(self):
        from scripts.analytics.research_intelligence import compact_monitor
        groups=[{'stock_code':str(i),'horizon_trading_days':5,'status':'pending',
            'review_required':False,'mature_observations':0} for i in range(40)]
        groups[-1].update(review_required=True,status='review_required',mature_observations=50)
        result=compact_monitor({'by_stock':groups,'by_horizon':[]})
        self.assertEqual(len(result['by_stock']),8)
        self.assertTrue(result['by_stock'][0]['review_required'])
        self.assertEqual(result['by_stock_details_omitted'],32)
        self.assertEqual(result['by_stock_status_counts'],{'review_required':1,'pending':39})


if __name__=='__main__':unittest.main()
