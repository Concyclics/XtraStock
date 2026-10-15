import unittest
from scripts.analytics.paper_execution import step,DataGap

class PaperTests(unittest.TestCase):
 def setUp(self):
  self.state={'cash':'100000','lots':[]}
  self.bar={'open':10,'high':10.5,'low':9.5,'close':10,'amount':100000000,'quality':'verified','limit_up':11,'limit_down':9,'lot_size':100}
  self.order={'id':'o1','code':'600000','side':'buy','qty':1000,'session':'2026-10-14','decision_at':'2026-10-13T14:00:00+00:00'}
  self.policy={'buy_fee_bps':10,'sell_fee_bps':15,'slippage_bps':0,'max_participation':.01}
 def test_cash_conservation_and_input_immutable(self):
  r=step(self.state,'2026-10-14',{'600000':self.bar},[self.order],self.policy)
  self.assertEqual(r['cash'],'89990.00');self.assertEqual(r['nav'],'99990.00');self.assertEqual(self.state['lots'],[])
 def test_repeat_day_rejected(self):
  r=step(self.state,'2026-10-14',{'600000':self.bar},[self.order],self.policy)
  with self.assertRaises(DataGap):step(r['state'],'2026-10-14',{'600000':self.bar})
 def test_future_decision_rejected(self):
  with self.assertRaises(DataGap):step(self.state,'2026-10-14',{'600000':self.bar},[{**self.order,'decision_at':'2026-10-14T08:00:00+08:00'}])
 def test_limit_no_fake_fill(self):
  r=step(self.state,'2026-10-14',{'600000':{**self.bar,'open':11,'high':11}},[self.order],self.policy)
  self.assertEqual(r['fills'],[]);self.assertEqual(r['orders'][0]['status'],'expired')
 def test_tplus1(self):
  s={'cash':'0','lots':[{'code':'600000','qty':1000,'bought_on':'2026-10-14'}]}
  r=step(s,'2026-10-14',{'600000':self.bar},[{**self.order,'side':'sell'}],self.policy)
  self.assertFalse(r['fills']);self.assertEqual(r['positions']['600000'],1000)
 def test_cash_limit_partial(self):
  r=step({'cash':'1500','lots':[]},'2026-10-14',{'600000':self.bar},[self.order],self.policy)
  self.assertEqual(r['fills'][0]['qty'],100);self.assertEqual(r['orders'][0]['status'],'partial_expired')
 def test_missing_held_price_blocks_not_zero(self):
  s={'cash':'0','lots':[{'code':'600000','qty':1000,'bought_on':'2026-10-12'}]}
  with self.assertRaises(DataGap):step(s,'2026-10-14',{})
 def test_dividend_receivable_not_early_cash(self):
  s={'cash':'0','lots':[{'code':'600000','qty':1000,'bought_on':'2026-10-12'}]}
  a={'id':'d','kind':'cash_dividend','code':'600000','ex_date':'2026-10-14','pay_date':'2026-10-16','quality':'verified','per_share':1}
  r=step(s,'2026-10-14',{'600000':{**self.bar,'close':9}},actions=[a])
  self.assertEqual(r['cash'],'0.00');self.assertEqual(r['receivable'],'1000.00');self.assertEqual(r['nav'],'10000.00')
  r2=step(r['state'],'2026-10-16',{'600000':{**self.bar,'close':9}})
  self.assertEqual(r2['cash'],'1000.00');self.assertEqual(r2['receivable'],'0.00')
 def test_duplicate_order_rejected(self):
  with self.assertRaises(DataGap):step(self.state,'2026-10-14',{'600000':self.bar},[self.order,self.order])
 def test_same_security_orders_share_capacity(self):
  r=step(self.state,'2026-10-14',{'600000':{**self.bar,'amount':1000000}},[self.order,{**self.order,'id':'o2'}],self.policy)
  self.assertEqual(sum(f['qty'] for f in r['fills']),1000)
 def test_sell_then_buy_shares_participation_budget(self):
  state={'cash':'100000','lots':[{'code':'600000','qty':1000,'bought_on':'2026-10-12'}]}
  r=step(state,'2026-10-14',{'600000':{**self.bar,'amount':1000000}},[self.order,{**self.order,'id':'sell','side':'sell'}],self.policy)
  self.assertEqual(len(r['fills']),1);self.assertEqual(r['fills'][0]['side'],'sell')
 def test_unknown_action_blocks(self):
  with self.assertRaises(DataGap):step(self.state,'2026-10-14',{},actions=[{'id':'a','kind':'rights','ex_date':'2026-10-14','code':'600000','quality':'verified'}])
if __name__=='__main__':unittest.main()
