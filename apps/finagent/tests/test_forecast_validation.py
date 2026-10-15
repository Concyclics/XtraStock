import copy
import datetime as dt
import unittest
import numpy as np
from scripts.analytics.forecast_validation import (block_interval, split_rows, fit_candidate,
    predict_candidate, calibrate, intervals, evaluate_fold, ranked_backtest, train_validated)
from scripts.analytics.forecast_evidence import validation_evidence
from scripts.analytics.reconstructed_factor_research import select_stable_factors, evaluate_rows
from scripts.analytics.research_history import history_plan


def sample(n=850, stocks=5, horizon=20):
    rng=np.random.default_rng(21); rows=[]; start=dt.date(2022,1,1)
    for i in range(n):
        for code in range(stocks):
            x=[float(rng.normal(0,.03)),float(rng.normal(0,.05)),.01+.005*code,-.02,float(rng.uniform(.8,1.2))]
            rows.append({'stock_code':str(code),'signal_date':str(start+dt.timedelta(days=i)),
                'entry_date':str(start+dt.timedelta(days=i+1)),'exit_date':str(start+dt.timedelta(days=i+horizon)),
                'x':x,'y':2*x[0]+float(rng.normal(0,.02))})
    return rows


class ForecastValidationTests(unittest.TestCase):
    def test_all_fold_boundaries_purge_actual_exit_dates(self):
        for h in (5,20,60):
            parts=split_rows(sample(1100,1,h),horizon=h)
            for a,b in zip(('train','tune','calibration'),('tune','calibration','test')):
                self.assertLess(max(r['exit_date'] for r in parts[a]),min(r['signal_date'] for r in parts[b]))
            self.assertGreaterEqual(len({r['signal_date'] for r in parts['calibration']}),5*h)

    def test_final_labels_cannot_change_selection_or_calibration(self):
        parts=split_rows(sample(),horizon=20); before=evaluate_fold(parts,20)
        changed=copy.deepcopy(parts)
        for r in changed['test']:r['y']+=.7
        after=evaluate_fold(changed,20)
        self.assertEqual(before['selected_spec'],after['selected_spec'])
        self.assertEqual(before['model'],after['model'])
        self.assertEqual(before['tuning_trials'],after['tuning_trials'])
        self.assertEqual(before['final_test']['selected_baseline'],after['final_test']['selected_baseline'])
        self.assertNotEqual(before['final_test']['mae'],after['final_test']['mae'])

    def test_calibration_labels_cannot_choose_hyperparameters(self):
        parts=split_rows(sample(),horizon=20); before=evaluate_fold(parts,20)
        changed=copy.deepcopy(parts)
        for r in changed['calibration']:r['y']+=.2
        after=evaluate_fold(changed,20)
        self.assertEqual(before['selected_spec'],after['selected_spec'])
        self.assertEqual(before['model']['beta'],after['model']['beta'])
        self.assertNotEqual(before['model']['normalized_residual_interval_80'],after['model']['normalized_residual_interval_80'])

    def test_intervals_follow_observed_risk_and_remain_disjoint_calibration(self):
        parts=split_rows(sample(),horizon=20)
        model=calibrate(fit_candidate(parts['train'],{'family':'ridge_raw','alpha':100,'window':252},20),parts['calibration'])
        rows=[{'x':[.01,.02,.01,-.01,1]},{'x':[.01,.02,.04,-.01,1]}]
        lo,hi=intervals(model,rows)
        self.assertAlmostEqual((hi-lo)[1]/(hi-lo)[0],4.)
        self.assertLess(model['trained_outcome_through'],model['calibration_signal_from'])
        self.assertTrue(np.isfinite(predict_candidate(model,rows)).all())

    def test_bootstrap_uses_dates_and_reports_few_monthly_blocks(self):
        r=block_interval(np.zeros(126),20)
        self.assertEqual(r['date_groups'],126);self.assertEqual(r['effective_blocks_approx'],6)
        self.assertEqual(r['lower_95'],0);self.assertEqual(r['upper_95'],0)
        with self.assertRaises(ValueError):block_interval([np.nan],5)

    def test_ranked_baskets_do_not_double_count_overlapping_holding_periods(self):
        rows=sample(21,5,5)
        for r in rows:r['y']=.1 if r['stock_code']=='0' else 0
        result=ranked_backtest(rows,[1 if r['stock_code']=='0' else 0 for r in rows],20)
        self.assertEqual(result['holding_windows'],5)
        self.assertAlmostEqual(result['windows'][0]['net_return'],.098)
        self.assertAlmostEqual(result['windows'][0]['equal_weight_net_return'],.018)
        self.assertAlmostEqual(result['mean_window_excess'],.08)
        self.assertFalse(result['executable_result'])
        for a,b in zip(result['windows'],result['windows'][1:]):self.assertLess(a['exit_date'],b['entry_date'])

    def test_duplicate_training_rows_are_rejected(self):
        rows=sample(10,1)
        with self.assertRaisesRegex(ValueError,'duplicate'):train_validated(rows+[rows[0]],20)

    def test_nonfinite_feature_or_label_is_not_silently_dropped(self):
        rows=sample(10,1); spec={'family':'ridge_raw','alpha':100,'window':None}
        rows[0]['y']=float('inf')
        with self.assertRaises(ValueError):fit_candidate(rows,spec,20)
        rows[0]['y']=0;rows[0]['x'][1]=float('nan')
        with self.assertRaises(ValueError):fit_candidate(rows,spec,20)

    def test_factor_correlation_pruning_and_unstable_signs(self):
        dates=[str(i) for i in range(90)]
        daily={d:{'stable':{'ic':.1+.01*np.sin(i)},'duplicate':{'ic':.2+.02*np.sin(i)},
                  'unstable':{'ic':.7 if i<30 else -.1}} for i,d in enumerate(dates)}
        selected,scored,redundant=select_stable_factors(daily,dates,['stable','duplicate','unstable'])
        self.assertEqual(len(selected),1);self.assertEqual(selected[0]['name'],'duplicate')
        self.assertEqual(redundant[0]['name'],'stable')
        self.assertFalse(next(r for r in scored if r['name']=='unstable')['eligible'])

    def test_short_monthly_tests_cannot_establish_factor_qualification(self):
        result=evaluate_rows(sample(500,8,20),20,history_plan(126,40,3,20))
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['validation_minimums']['test_sessions_per_fold'],100)
        self.assertFalse(result['research_signal_validated'])

    def test_constant_test_labels_remain_explicit_and_json_safe(self):
        import json
        rows=sample(400,8,5)
        first=sorted({r['signal_date'] for r in rows})[-40]
        for r in rows:
            if r['signal_date']>=first:r['y']=0
        result=evaluate_rows(rows,5,history_plan(126,40,2,5))
        self.assertIsNone(result['folds'][-1]['test_rank_ic'])
        self.assertIsNone(result['folds'][-1]['legacy_test_rank_ic'])
        self.assertEqual(result['status'],'insufficient_variation')
        json.dumps(result,allow_nan=False)

    def test_public_evidence_keeps_units_and_failure_reasons_without_model_internals(self):
        result=train_validated(sample(),20)
        r=validation_evidence({'horizon':20,'as_of':'2026-10-14','created_at':'2026-10-14T12:00:00+00:00',
            'evaluation':result['evaluation']})
        self.assertEqual(r['average_error_percentage_points'],round(result['evaluation']['final_test']['mae']*100,3))
        self.assertFalse(r['prediction_eligible']);self.assertNotIn('beta',str(r))
        self.assertTrue(r['updated_at'].endswith('+08:00'))
        self.assertIn('不是上涨概率',r['interpretation'])


if __name__=='__main__':unittest.main()
