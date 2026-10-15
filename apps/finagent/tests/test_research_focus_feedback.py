"""Feedback changes research follow-up; it never promotes a stock or invents maturity."""
import copy
import datetime as dt
import unittest
from scripts.analytics.research_focus import build_focus
from tests.test_research_focus import NOW,event
from scripts.analytics.research_focus_feedback import focus_feedback_context,collect_focus_feedback,feedback_subjects
from unittest.mock import MagicMock,patch

def feedback(n=0,review=False):
    return {'availability':'available','mature_observations':n,'review_required':review,
            'next_checks':['核查预测误差超过简单基线的原因，复核原始行情与输入。'] if review else [],
            'horizons':[{'horizon_trading_days':5,'mature_observations':n,'status':'review_required' if review else 'pending'}]}

class FocusFeedbackTests(unittest.TestCase):
    def test_zero_maturity_does_not_change_score_state_or_version_reason(self):
        previous,_=build_focus([event()],{},[],NOW)
        baseline,_=build_focus([event()],{},previous,NOW+dt.timedelta(minutes=1))
        rows,_=build_focus([event()],{'000001':{'forecast_feedback':feedback()}},previous,NOW+dt.timedelta(minutes=1))
        for key in ('priority','state','change','next_review_at','last_material_change_at'):
            self.assertEqual(rows[0][key],baseline[0][key],key)
        self.assertFalse(rows[0]['feedback_changed']);self.assertIn('为 0',''.join(rows[0]['reasons']))

    def test_mature_review_is_recorded_without_investment_score_promotion(self):
        previous,_=build_focus([event()],{},[],NOW)
        baseline,_=build_focus([event()],{},previous,NOW+dt.timedelta(minutes=1))
        contexts={'000001':{'forecast_feedback':feedback(25,True)}}
        rows,_=build_focus([event()],contexts,previous,NOW+dt.timedelta(minutes=1))
        self.assertEqual(rows[0]['priority'],baseline[0]['priority']);self.assertEqual(rows[0]['state'],baseline[0]['state'])
        self.assertEqual(rows[0]['change'],'feedback_updated');self.assertTrue(rows[0]['feedback_changed'])
        self.assertIn('核查预测误差',rows[0]['next_checks'][0]);self.assertFalse(rows[0]['prediction_eligible'])
        repeat,_=build_focus([event()],contexts,rows,NOW+dt.timedelta(minutes=2))
        self.assertEqual(repeat[0]['change'],'retained');self.assertFalse(repeat[0]['feedback_changed'])
        self.assertEqual(repeat[0]['next_review_at'],rows[0]['next_review_at'])

    def test_small_sample_records_context_without_review_task(self):
        previous,_=build_focus([event()],{},[],NOW)
        rows,_=build_focus([event()],{'000001':{'forecast_feedback':feedback(2)}},previous,NOW)
        self.assertFalse(rows[0]['context']['forecast_feedback']['review_required'])
        self.assertFalse(any('预测误差超过' in s for s in rows[0]['next_checks']))
        self.assertTrue(any('不据此宣称预测可靠' in s for s in rows[0]['reasons']))

    def test_feedback_cannot_create_an_unrequested_stock(self):
        rows,_=build_focus([event()],{'000002':{'forecast_feedback':feedback(50,True)}},[],NOW)
        self.assertEqual([r['focus_key'] for r in rows],['stock:000001'])

    def test_unavailable_is_not_claimed_as_zero(self):
        previous,_=build_focus([event()],{},[],NOW)
        state={'availability':'unavailable','mature_observations':None,'review_required':False}
        rows,_=build_focus([event()],{'000001':{'forecast_feedback':state}},previous,NOW)
        self.assertFalse(rows[0]['feedback_changed'])
        self.assertTrue(any('无法读取' in s for s in rows[0]['reasons']))
        self.assertFalse(any('反馈为 0' in s for s in rows[0]['reasons']))

    def test_failed_read_and_recovery_do_not_invent_new_mature_results(self):
        context={'000001':{'forecast_feedback':feedback(25,True)}}
        before,_=build_focus([event()],context,[],NOW)
        unavailable={'000001':{'forecast_feedback':{'availability':'unavailable','mature_observations':None}}}
        failed,_=build_focus([event()],unavailable,before,NOW+dt.timedelta(minutes=1))
        recovered,_=build_focus([event()],context,failed,NOW+dt.timedelta(minutes=2))
        self.assertFalse(failed[0]['feedback_changed']);self.assertFalse(recovered[0]['feedback_changed'])

    def test_monitor_poll_time_and_private_lineage_do_not_change_semantics(self):
        row={'stock_code':'000001','horizon_trading_days':5,'status':'review_required','mature_observations':25,
             'signal_dates':25,'independent_windows':5,'recent':{'mae':.03,'checked_at':'first','source_digest':'a'*64},
             'review_reasons':['误差高于简单基线']}
        monitor={'status':'available','checked_at':'first','coverage':{'queried_stock_codes':['000001']},'by_stock':[row]}
        initial=focus_feedback_context(monitor,['000001'])
        monitor['checked_at']='second';row['recent']['checked_at']='second';row['recent']['source_digest']='b'*64
        self.assertEqual(initial,focus_feedback_context(monitor,['000001']))
        self.assertTrue(initial['000001']['review_required'])
        self.assertNotIn('source_digest',str(initial))

    def test_small_or_overlapping_monitor_groups_cannot_create_review(self):
        row={'stock_code':'000001','horizon':5,'status':'review_required','mature_observations':100,
             'signal_dates':25,'independent_windows':4,'review_reasons':['误差较高']}
        monitor={'status':'available','coverage':{'queried_stock_codes':['000001']},'by_stock':[row]}
        self.assertFalse(focus_feedback_context(monitor,['000001'])['000001']['review_required'])
        row.update(independent_windows=5,signal_dates=19)
        self.assertFalse(focus_feedback_context(monitor,['000001'])['000001']['review_required'])

    def test_truncated_or_missing_stocks_are_not_counted_as_zero(self):
        monitor={'status':'pending','coverage':{'queried_stock_codes':['000001'],'truncated':True},
                 'by_stock':[{'stock_code':'000001','horizon':5,'status':'pending','mature_observations':0}]}
        result=focus_feedback_context(monitor,['000001','000002'])
        self.assertEqual(result['000001']['mature_observations'],0);self.assertNotIn('000002',result)
        unknown=focus_feedback_context({'status':'unavailable'},['000001'])
        self.assertIsNone(unknown['000001']['mature_observations'])

    def test_collector_passes_original_cutoff_and_uses_only_bounded_subjects(self):
        conn=MagicMock();codes=[str(i).zfill(6) for i in range(25)]
        with patch('scripts.analytics.learning_monitor.load_prospective_monitor',return_value={
            'status':'pending','coverage':{'queried_stock_codes':codes[:20]},'by_stock':[]}) as monitor:
            collect_focus_feedback(conn,NOW,NOW.date(),codes)
        monitor.assert_called_once_with(conn,NOW,NOW.date(),codes[:20],limit=20)

    def test_collector_failure_keeps_count_unknown_and_omits_exception_details(self):
        with patch('scripts.analytics.learning_monitor.load_prospective_monitor',side_effect=RuntimeError('private SQL')):
            contexts,meta=collect_focus_feedback(MagicMock(),NOW,NOW.date(),['000001'])
        self.assertEqual(meta['status'],'unavailable');self.assertIsNone(meta['mature_observations'])
        self.assertIsNone(contexts['000001']['mature_observations']);self.assertNotIn('private SQL',str(meta))

    def test_theme_members_cannot_displace_existing_company_subjects(self):
        previous=[{'focus_key':'theme:1','entity_type':'theme','state':'active','priority':100,'stock_codes':['000099']},
                  {'focus_key':'stock:000001','entity_type':'stock','state':'active','priority':30,'stock_codes':['000001']},
                  {'focus_key':'stock:000002','entity_type':'stock','state':'archived','priority':90,'stock_codes':['000002']}]
        self.assertEqual(feedback_subjects(previous),['000001'])

if __name__=='__main__':unittest.main()
