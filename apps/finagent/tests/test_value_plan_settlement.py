import unittest
from scripts.analytics.settle_value_plan_paper import orders_for_session
from scripts.analytics.value_plan import load_config

class SettlementTests(unittest.TestCase):
    def plan(self):
        return dict(plan_id='p',stock_code='600000',simulation_eligible=True,entry_limit=10,entry_expires_on='2026-10-20',
            decision_at='2026-10-16T08:50:00+08:00',published_at='2026-10-16T09:05:00+08:00')
    def test_duplicate_pending_plan_is_not_reissued_with_full_quantity(self):
        p=self.plan();cfg=load_config();dates=['2026-10-16','2026-10-19','2026-10-20']
        orders,_=orders_for_session({'lots':[]},[p],dates[0],dates,{},cfg)
        original={**orders[0],'qty':100}
        next_orders,_=orders_for_session({'lots':[],'pending_limits':[original]},[p],dates[1],dates,{},cfg)
        self.assertEqual(len(next_orders),1);self.assertEqual(next_orders[0]['qty'],100)
        self.assertEqual(next_orders[0]['id'],original['id']);self.assertEqual(next_orders[0]['expires_on'],'2026-10-20')
    def test_late_internal_plan_never_fills_same_day(self):
        p={**self.plan(),'published_at':'2026-10-16T10:00:00+08:00'}
        orders,_=orders_for_session({'lots':[]},[p],'2026-10-16',['2026-10-16','2026-10-19'],{},load_config())
        self.assertEqual(orders,[])
    def test_expired_or_removed_interest_is_cancelled(self):
        state={'lots':[],'pending_limits':[dict(code='600000',plan_id='old',id='order',expires_on='2026-10-15')]}
        orders,events=orders_for_session(state,[],'2026-10-16',['2026-10-16'],{},load_config())
        self.assertEqual(orders,[]);self.assertEqual(events[0]['event_type'],'cancelled')
    def test_existing_holding_is_not_rebought(self):
        state={'lots':[dict(code='600000',qty=100,bought_on='2026-10-15')]}
        orders,_=orders_for_session(state,[self.plan()],'2026-10-16',['2026-10-15','2026-10-16'],{},load_config())
        self.assertFalse(orders)

if __name__=='__main__':unittest.main()
