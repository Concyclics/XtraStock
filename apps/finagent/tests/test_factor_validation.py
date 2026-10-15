import datetime as dt
import unittest
from scripts.analytics.factor_validation import independent_windows, purged_folds, hac_p_value, VALIDATION_VERSION
from scripts.analytics.evaluate_factors import passed

class FactorValidationTests(unittest.TestCase):
    def row(self, start, end, horizon=20, version=1):
        return dict(test_start=start,test_end=end,horizon_days=horizon,status="complete",passed_gates=True,
                    rank_ic_mean=.04,metrics=dict(validation_version=VALIDATION_VERSION,cost_adjusted=True,
                    input_contract_version="pit-input-v1",strict_pit_ready=True,
                    execution_constraints="bars",net_spread_mean=.01,factor_version=version,outcome_end=end))

    def test_overlapping_rolling_windows_are_not_independent(self):
        rows=[self.row("2025-01-01","2025-12-31"),self.row("2025-02-01","2026-01-31"),
              self.row("2026-01-01","2026-03-31")]
        self.assertEqual(len(independent_windows(rows,1)),2)
        self.assertEqual(len(independent_windows(rows,2)),0)

    def test_horizons_and_label_overlap_do_not_inflate_count(self):
        a=self.row("2025-01-01","2025-03-31")
        a["metrics"]["outcome_end"]="2025-04-30"
        rows=[a,self.row("2025-04-01","2025-06-30"),self.row("2025-07-01","2025-09-30",5)]
        self.assertEqual(len(independent_windows(rows,1)),1)

    def test_legacy_evaluation_is_not_new_validation(self):
        row=self.row("2025-01-01","2025-03-31")
        row["metrics"]={"horizon_days":20}
        self.assertEqual(independent_windows([row]),[])

    def test_folds_group_dates_and_purge_boundary(self):
        dates=[dt.date(2020,1,1)+dt.timedelta(days=i) for i in range(1000)]
        folds=purged_folds(dates,3,80,20)
        self.assertEqual(len(folds),3)
        for train_start,train_end,test_start,test_end in folds:
            self.assertGreaterEqual((test_start-train_end).days,21)
        self.assertLess(folds[0][3],folds[1][2]-dt.timedelta(days=19))

    def test_hac_rejects_too_few_independent_observations(self):
        self.assertIsNone(hac_p_value([.1]*20,19))
        self.assertIsNotNone(hac_p_value([.01,.02,-.01,.03]*30,4))

    def test_cost_negative_result_never_passes(self):
        metrics=dict(date_count=200,coverage=.9,rank_ic_mean=.1,rank_ic_ir=.5,
             sign_consistency=.8,top_bottom_spread_mean=.001,turnover_mean=.1,
             cost_adjusted=True,execution_constraints="bars",net_spread_mean=-.009)
        self.assertFalse(passed(metrics,.01,{}))
