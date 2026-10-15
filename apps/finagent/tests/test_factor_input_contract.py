import datetime as dt
import unittest
from scripts.analytics.factor_input_contract import eligible_mining_fields, require_stock_horizon
from scripts.analytics.factor_dsl import compile_expression
from scripts.analytics.evaluate_factors import passed
from scripts.analytics.factor_validation import independent_windows, VALIDATION_VERSION


class InputContractTests(unittest.TestCase):
    def test_miner_refuses_missing_and_constant_inputs(self):
        ready,skip=eligible_mining_fields({'flow':{'total':100,'observed':0,'stddev':None},
            'constant':{'total':100,'observed':100,'stddev':0},
            'covered':{'total':100,'observed':90,'stddev':1},
            'sparse':{'total':100,'observed':1,'stddev':1}})
        self.assertEqual(ready,['covered']);self.assertEqual(set(skip),{'flow','constant','sparse'})

    def test_stock_same_day_exit_is_diagnostic_only(self):
        with self.assertRaisesRegex(ValueError,'T\\+1'):require_stock_horizon(1)
        for h in (5,20,60):require_stock_horizon(h)

    def test_reconstructed_inputs_cannot_pass_production_gate(self):
        base=dict(input_contract_version='pit-input-v1',strict_pit_ready=True,date_count=120,
            cost_adjusted=True,execution_constraints='bars',net_spread_mean=.02,coverage=.95,
            rank_ic_mean=.05,rank_ic_ir=.5,sign_consistency=.75,top_bottom_spread_mean=.03,turnover_mean=.2)
        self.assertTrue(passed(base,.01,{}))
        self.assertFalse(passed({**base,'strict_pit_ready':False},.01,{}))
        self.assertFalse(passed({**base,'input_contract_version':'legacy'},.01,{}))

    def test_old_or_reconstructed_windows_never_count(self):
        row=dict(test_start='2024-01-01',test_end='2024-05-01',horizon_days=5,status='complete',passed_gates=True,
            rank_ic_mean=.1,metrics=dict(validation_version=VALIDATION_VERSION,cost_adjusted=True,
            execution_constraints='bars',net_spread_mean=.02,factor_version=1,
            input_contract_version='pit-input-v1',strict_pit_ready=False))
        self.assertEqual(independent_windows([row]),[])
        row['metrics']['strict_pit_ready']=True
        self.assertEqual(len(independent_windows([row])),1)
        row['metrics']['validation_version']='purged-cost-v2'
        self.assertEqual(independent_windows([row]),[])

if __name__=='__main__':unittest.main()
