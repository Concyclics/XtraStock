import copy
import datetime as dt
import unittest
from scripts.analytics.collect_earnings_observations import observations
from scripts.analytics.limit_execution import match_limit,exit_barrier,first_eligible_session
from scripts.analytics.paper_execution import step
from scripts.analytics.value_plan import balanced_consensus,valuations,build_plans,load_config,qualify,render


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.bar=dict(open=10,high=11,low=9,close=10,limit_up=11,limit_down=9,lot_size=100,amount=10000000,quality='verified')
        self.order=dict(id='a',code='600000',side='buy',qty=1000,limit_price='9.50',session='2026-10-16',
            decision_at='2026-10-16T08:50:00+08:00',published_at='2026-10-16T09:05:00+08:00')
    def test_limit_is_enforced_not_next_open(self):
        self.assertEqual(float(match_limit(self.order,self.bar,'2026-10-16')[0]),9.5)
        self.assertEqual(match_limit(self.order,{**self.bar,'low':9.5},'2026-10-16'),(None,'limit_not_penetrated'))
    def test_price_improvement_and_adverse_slippage_cannot_cross_limit(self):
        self.assertEqual(float(match_limit({**self.order,'limit_price':10.5},self.bar,'2026-10-16',5)[0]),10.01)
    def test_late_delivery_cannot_fill_earlier(self):
        with self.assertRaises(ValueError):match_limit({**self.order,'published_at':'2026-10-16T09:16:00+08:00'},self.bar,'2026-10-16')
        self.assertEqual(first_eligible_session('2026-10-16T09:16:00+08:00',['2026-10-16','2026-10-19']),'2026-10-19')
    def test_timezone_and_publish_order(self):
        for value in ('2026-10-16T09:00:00','2026-10-16T08:00:00+08:00'):
            with self.assertRaises(ValueError):match_limit({**self.order,'published_at':value},self.bar,'2026-10-16')
    def test_locked_and_expired(self):
        self.assertEqual(match_limit(self.order,{**self.bar,'open':11,'low':11,'close':11},'2026-10-16')[1],'locked_price_limit')
        self.assertEqual(match_limit({**self.order,'expires_on':'2026-10-15'},self.bar,'2026-10-16')[1],'expired')
    def test_two_barriers_does_not_choose_profit(self):
        self.assertEqual(exit_barrier(self.bar,9.5,10.5,'2026-10-15','2026-10-16')[1],'both_touched_adverse')
        self.assertEqual(exit_barrier(self.bar,9.5,10.5,'2026-10-16','2026-10-16')[1],'t_plus_one')
    def test_limit_step_partial_and_basis(self):
        r=step({'cash':1000,'lots':[]},'2026-10-16',{'600000':self.bar},[self.order],
            dict(buy_fee_bps=15,sell_fee_bps=15,slippage_bps=0,max_participation=.01,track_cost_basis=True))
        self.assertEqual(r['orders'][0]['status'],'partial_expired')
        self.assertEqual(r['fills'][0]['price'],'9.50');self.assertEqual(r['state']['lots'][0]['cost_cash'],'951.43')
    def test_intraday_sell_cannot_fund_earlier_buy(self):
        state={'cash':0,'lots':[dict(code='600001',qty=1000,bought_on='2026-10-15')]}
        orders=[self.order,{**self.order,'id':'s','side':'sell','code':'600001','limit_price':10.5}]
        r=step(state,'2026-10-16',{'600000':self.bar,'600001':self.bar},orders,
            dict(buy_fee_bps=15,sell_fee_bps=15,slippage_bps=0,max_participation=.01,no_intraday_sale_funding=True))
        self.assertEqual([f['side'] for f in r['fills']],['sell'])


class ValuationTests(unittest.TestCase):
    def obs(self,**kw):
        return dict(stock_code='600000',institution='机构一',document_id='doc',report_date='2026-09-01',forecast_year=2026,
            metric='eps',value=2,unit='CNY/share',source_basis='explicit_dated_report_text',
            known_at='2026-09-02T00:00:00+08:00',observed_at='2026-09-02T00:00:00+08:00',**kw)
    def test_rolling_api_never_backdated_or_wrong_year(self):
        r=observations({'currentYear':2026,'data':[dict(stockCode='600000',infoCode='AP20261016000001',publishDate='2022-01-01',predictThisYearEps='2')]},
            '2026-10-16T00:00:00+00:00','hash')[0]
        self.assertEqual(r['forecast_year'],2026)
        self.assertEqual(balanced_consensus([dict(r,document_id='d')],'2026-10-15T15:00:00+08:00',2026)['institutions'],0)
    def test_each_institution_votes_once_and_zero_invalidates_older(self):
        rows=[self.obs(),{**self.obs(),'document_id':'new','report_date':'2026-10-01','value':0},
            {**self.obs(),'institution':'机构二','value':4}]
        c=balanced_consensus(rows,'2026-10-16T15:00:00+08:00',2026)
        self.assertEqual(c['institutions'],2);self.assertEqual(c['eps_median'],2)
    def test_conflict_and_split_are_not_silently_used(self):
        c=balanced_consensus([self.obs(),{**self.obs(),'value':3}],'2026-10-16T15:00:00+08:00',2026)
        self.assertIsNone(c['eps_median']);self.assertTrue(c['conflicts'])
        c=balanced_consensus([self.obs()],'2026-10-16T15:00:00+08:00',2026,actions=[dict(ex_date='2026-10-01',changes_share_basis=True)])
        self.assertEqual(c['institutions'],0)
    def test_revision_is_same_institution_and_fiscal_year(self):
        c=balanced_consensus([self.obs(),{**self.obs(),'document_id':'new','report_date':'2026-10-01','value':3}],
            '2026-10-16T15:00:00+08:00',2026)
        self.assertEqual(c['revision_median'],.5)
        c=balanced_consensus([self.obs(),{**self.obs(),'document_id':'new','report_date':'2026-10-01','value':3,'forecast_year':2027}],
            '2026-10-16T15:00:00+08:00',2026)
        self.assertIsNone(c['revision_median'])
    def test_api_refresh_is_not_earnings_revision(self):
        c=balanced_consensus([self.obs(),{**self.obs(),'document_id':'new','report_date':'2026-10-01','value':3,'source_basis':'observed_api_current_year'}],
            '2026-10-16T15:00:00+08:00',2026)
        self.assertIsNone(c['revision_median'])
    def test_price_recomputed_and_finance_sector_not_forced(self):
        cfg=load_config();cfg['min_industry_peers']=2
        c=dict(eps_median=2,eps_low=1.8,eps_high=2.2,institutions=3,conflicts=[],fiscal_year=2026)
        rows=[dict(stock_code=str(i),industry='工业设备',close=20+i,consensus=c) for i in range(3)]
        v=valuations(rows,cfg);self.assertEqual(v[0]['forward_pe'],10);self.assertIsNotNone(v[0]['fair_value'])
        rows[0]['industry']='银行';self.assertIsNone(valuations(rows,cfg)[0]['fair_value'])
    def test_shadow_plan_never_promises_return_or_hit_date(self):
        cfg=load_config();c=balanced_consensus([self.obs()],'2026-10-16T15:00:00+08:00',2026)
        row=dict(stock_code='600000',name='示例',industry='设备',consensus=c,close=20,market_date='2026-10-15',financial_eligible=True)
        rows=valuations([row],cfg)
        p=build_plans(rows,[],'2026-10-16T15:00:00+08:00','2026-10-16T15:00:00+08:00',['2026-10-19'],cfg)[0]
        self.assertIsNone(p['expected_net_return']);self.assertIsNone(p['expected_sell_date'])
        self.assertNotEqual(p['state'],'eligible_to_buy');self.assertNotIn('买入上限',render([p],'2026-10-16'))
    def test_removal_does_not_turn_existing_holding_into_sale(self):
        old=[dict(stock_code='600000',plan_id='p',state='held')]
        p=build_plans([],old,'2026-10-16T15:00:00+08:00','2026-10-16T15:00:00+08:00',[],load_config())[0]
        self.assertEqual(p['state'],'data_review');self.assertIsNone(p['entry_limit'])
        self.assertFalse(p['simulation_eligible'])
    def test_good_pnl_cannot_override_missing_pit_and_calibration(self):
        m=dict(cagr=.30,maximum_drawdown=.1,period_win_rate=.7,annual={str(y):dict(return_=.1) for y in []})
        m['annual']={str(y):{'return':.1,'complete':True} for y in range(2022,2026)}
        r=qualify(m,dict(cagr=.1,period_win_rate=.5),{},20,load_config())
        self.assertFalse(r['passed']);self.assertIn('point_in_time',r['failed'])

if __name__=='__main__':unittest.main()
