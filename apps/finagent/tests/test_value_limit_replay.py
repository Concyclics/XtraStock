import unittest
import numpy as np
from scripts.analytics.value_limit_replay import replay

class ValueReplayTests(unittest.TestCase):
    def data(self,n=30):
        dates=np.array([str(d) for d in np.arange(np.datetime64('2026-01-01'),np.datetime64('2026-01-01')+n)])
        d={k:np.full((n,1),v,dtype=float) for k,v in dict(open=10,high=10.4,low=9.5,close=10,amount=10000000,volume=10000).items()}
        d.update(dates=dates,symbols=np.array(['600001']),actions=[],primary_since=dates[1])
        f={'valid':np.ones((n,1),bool),'adv':np.full((n,1),10000000)}
        return d,f,np.ones((n,1)),np.full((n,1),9.9)
    def test_fills_obey_frozen_limit_and_cashbook(self):
        d,f,s,l=self.data();r=replay(d,f,s,l,d['dates'][1],d['dates'][-1],size=1)
        for fill in r['fills']:
            if fill['side']=='buy':self.assertLessEqual(fill['price'],fill['limit_price'])
            else:self.assertGreaterEqual(fill['price'],fill['limit_price'])
            self.assertLessEqual(fill['fill_qty'],fill['daily_volume_shares']*fill['participation']+1e-6)
        for row in r['daily_account']:
            self.assertGreaterEqual(row['cash'],-.01)
            self.assertAlmostEqual(row['nav'],row['cash']+row['positions']+row['receivable'],places=5)
        self.assertFalse(r['production_approved'])
    def test_not_hit_is_never_filled_and_expiry_preserved(self):
        d,f,s,l=self.data();l[:]=9
        r=replay(d,f,s,l,d['dates'][1],d['dates'][-1],size=1)
        self.assertFalse(r['fills']);self.assertEqual(r['metrics']['cagr'],0)
        self.assertTrue(any(e.get('reason')=='expired' for e in r['events']))
    def test_future_price_change_does_not_change_prior_orders(self):
        d,f,s,l=self.data();r=replay(d,f,s,l,d['dates'][1],d['dates'][-1],size=1)
        changed={**d,**{k:v.copy() for k,v in d.items() if isinstance(v,np.ndarray)}}
        for k in ('open','high','low','close'):changed[k][-1]*=1.05
        r2=replay(changed,f,s,l,d['dates'][1],d['dates'][-1],size=1)
        before=lambda r:[x for x in r['events'] if x['event_date']<d['dates'][-1]]
        self.assertEqual(before(r),before(r2))
    def test_missing_held_mark_remains_visible(self):
        d,f,s,l=self.data();f['valid'][10:]=False;d['close'][10:]=np.nan
        r=replay(d,f,s,l,d['dates'][1],d['dates'][-1],size=1)
        self.assertGreater(r['metrics']['terminal_stale_value'],0)
        self.assertGreater(r['metrics']['maximum_stale_sessions'],0)

if __name__=='__main__':unittest.main()
