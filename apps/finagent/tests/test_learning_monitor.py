import datetime as dt
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'/'analytics'))
from learning_monitor import monitor_group,readable_metrics,load_prospective_monitor,UNITS


def rows(n,horizon=5,offset=0,estimate=.019,actual=.02):
    start=dt.date(2024,1,1)
    values=[]
    for i in range(offset,offset+n):
        day=start+dt.timedelta(days=i)
        interval=[-.05,.05] if i%5 else [.03,.04]
        values.append({'stock_code':'600036','horizon':horizon,'signal_date':day,
            'entry_date':day+dt.timedelta(days=1),'exit_date':day+dt.timedelta(days=horizon),
            'expected_gross_return':estimate,'momentum_baseline':.015,'interval_80':interval,
            'outcome_due':True,'observation_valid_at_cutoff':True,
            'result':{'realized_gross_return':actual,'return_basis':'next_open_to_horizon_close_total_return_proxy'}})
    return values


class LearningMonitorTests(unittest.TestCase):
    def test_units_are_error_percentage_points_and_nominal_interval_coverage(self):
        value=readable_metrics({'mae':.025,'zero_return_mae':.02,'momentum_mae':.03,'coverage_80':.7655})
        self.assertEqual(value['mae_percentage_points'],2.5)
        self.assertIn('2.500 个百分点',value['mae_text'])
        self.assertAlmostEqual(value['coverage_80_percent'],76.55)
        self.assertIn('名义80%',value['coverage_80_text'])
        self.assertIn('非盈利或方向概率',value['coverage_80_text'])
        self.assertEqual(UNITS['nominal_interval_coverage'],.8)

    def test_zero_real_samples_is_pending_not_pass_or_review(self):
        r=monitor_group([],5,'600036')
        self.assertEqual(r['status'],'pending')
        self.assertEqual(r['mature_observations'],0)
        self.assertFalse(r['review_required']);self.assertFalse(r['production_approved'])
        self.assertIn('历史留出样本不计入',r['status_text'])

    def test_many_stocks_one_signal_date_never_replace_independent_days(self):
        source=rows(1,estimate=9)
        duplicated=[{**source[0],'stock_code':str(i)} for i in range(1000)]
        r=monitor_group(duplicated,5)
        self.assertEqual(r['signal_dates'],1)
        self.assertEqual(r['independent_windows'],1)
        self.assertEqual(r['status'],'pending');self.assertEqual(r['review_reasons'],[])

    def test_twenty_dates_but_four_independent_holding_windows_still_pending(self):
        r=monitor_group(rows(20,estimate=9),5)
        self.assertEqual(r['signal_dates'],20)
        self.assertEqual(r['independent_windows'],4)
        self.assertEqual(r['status'],'pending');self.assertFalse(r['review_required'])

    def test_sufficient_real_data_monitors_without_production_approval(self):
        r=monitor_group(rows(25),5)
        self.assertEqual(r['status'],'monitoring')
        self.assertEqual(r['recent']['independent_windows'],5)
        self.assertFalse(r['production_approved']);self.assertIsNone(r['changes'])
        self.assertIn('comparison_pending_reason',r)

    def test_horizon_aware_window_can_accumulate_five_twenty_day_windows(self):
        r=monitor_group(rows(100,horizon=20),20)
        self.assertEqual(r['recent']['signal_dates'],100)
        self.assertEqual(r['recent']['independent_windows'],5)
        self.assertEqual(r['status'],'monitoring')

    def test_large_errors_and_coverage_trigger_review_only_after_sufficiency(self):
        r=monitor_group(rows(25,estimate=.08),5)
        self.assertEqual(r['status'],'review_required')
        self.assertTrue(any('零收益基线' in reason for reason in r['review_reasons']))
        self.assertFalse(r['production_approved'])
        for row in (items:=rows(25)):row['interval_80']=[.05,.06]
        r=monitor_group(items,5)
        self.assertTrue(any('覆盖率' in reason for reason in r['review_reasons']))

    def test_latest_result_contract_or_time_failure_not_counted_as_mature(self):
        for variant in ('future_time','wrong_basis','nonfinite','reversed_interval'):
            items=rows(25)
            for row in items:
                if variant=='future_time':row['observation_valid_at_cutoff']=False
                elif variant=='wrong_basis':row['result']['return_basis']='historical_holdout'
                elif variant=='nonfinite':row['result']['realized_gross_return']=float('nan')
                else:row['interval_80']=[.2,.1]
            r=monitor_group(items,5)
            self.assertEqual(r['mature_observations'],0)
            self.assertEqual(r['invalid_observed_results'],25)
            self.assertEqual(r['status'],'pending');self.assertEqual(r['review_reasons'],[])

    def test_prior_period_purged_and_deterioration_is_explicit(self):
        prior=rows(35);recent=rows(25,offset=35,estimate=.08)
        r=monitor_group(prior+recent,5)
        self.assertTrue(r['prior']['sufficient'])
        self.assertLess(dt.date.fromisoformat(r['prior']['signal_end'])+dt.timedelta(days=5),
                        dt.date.fromisoformat(r['recent']['signal_start']))
        self.assertGreater(r['changes']['mae_change_percentage_points'],.5)
        self.assertTrue(any('较前期恶化' in reason for reason in r['review_reasons']))

    def test_zero_baseline_does_not_divide_by_zero(self):
        r=monitor_group(rows(25,estimate=.01,actual=0),5)
        self.assertEqual(r['recent']['zero_return_mae'],0)
        self.assertEqual(r['status'],'review_required')

    def test_row_truncation_disables_alarm_instead_of_using_partial_windows(self):
        r=monitor_group(rows(100,estimate=9),5,truncated=True)
        self.assertEqual(r['status'],'pending')
        self.assertFalse(r['review_required']);self.assertEqual(r['review_reasons'],[])

    def test_stored_error_and_coverage_flags_cannot_override_frozen_prediction(self):
        items=rows(25,estimate=.08)
        for row in items:row['result'].update(absolute_error=0,interval_covered=True)
        r=monitor_group(items,5)
        self.assertAlmostEqual(r['recent']['mae'],.06)
        self.assertAlmostEqual(r['recent']['coverage_80'],.8)

    def test_coverage_review_bounds_are_inclusive(self):
        for covered,expected_review in ((64,True),(65,False),(95,False),(96,True)):
            with self.subTest(covered=covered):
                items=rows(100,horizon=20)
                for i,row in enumerate(items):row['interval_80']=[-.05,.05] if i<covered else [.03,.04]
                r=monitor_group(items,20)
                self.assertEqual(r['review_required'],expected_review)

    def test_missing_schema_means_unknown_not_zero(self):
        class Connection:
            def execute(self,*args):return self
            def fetchone(self):return {'t':None}
        r=load_prospective_monitor(Connection(),dt.datetime.now(dt.timezone.utc),stock_codes=['600036'])
        self.assertEqual(r['status'],'unavailable')
        self.assertIsNone(r['mature_observations'])
        self.assertEqual(r['by_stock'],[])

if __name__=='__main__':unittest.main()
