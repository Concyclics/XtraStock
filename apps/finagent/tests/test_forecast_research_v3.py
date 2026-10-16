import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from forecast_research_v3 import forecast_features, public_date, select_fraction, require_training_support

class ForecastResearchTest(unittest.TestCase):
    def test_empty_warmup_window_cannot_pick_zero_return_tie(self):
        dates=np.asarray(['2020-11-30','2021-01-04'])
        eligible=np.asarray([[False]*25,[True]*25])
        with self.assertRaisesRegex(ValueError,'No effective training data'):
            require_training_support(dates,eligible,[{'start':'2020-07-01','end':'2020-11-30'}])

    def test_future_support_does_not_count_toward_training(self):
        dates=np.asarray(['2020-11-27','2020-11-30','2021-01-04'])
        eligible=np.asarray([[True]*25,[False]*25,[True]*25])
        result=require_training_support(dates,eligible,[{'start':'2020-07-01','end':'2020-11-30'}])
        self.assertEqual(result[0]['usable_sessions'],1)

    def dataset(self):
        return {'dates':np.asarray(['2020-12-28','2020-12-29','2020-12-30','2020-12-31','2021-01-04','2021-01-05','2021-01-06']),
                'symbols':np.asarray(['600001']), 'close':np.full((7,1),10.),'split_factor':np.ones((7,1))}

    def row(self,date,eps,fy=2021,broker='A',doc=None):
        return {'stock_code':'600001','institution':broker,'eps':eps,'forecast_year':fy,'report_date':date,
          'published_at':date+'T00:00:00+08:00','document_id':doc or date+str(fy)+broker,
          'metadata':{'semantic_contract':'earnings-forecast-v2','year_verified':True,'year_basis':'explicit_dated_report_text','eps_unit':'CNY/share'}}

    def test_no_feature_before_publication_delay(self):
        d=self.dataset();f,a=forecast_features(d,[self.row('2020-12-28',1),self.row('2020-12-30',1.2)])
        self.assertTrue(np.isnan(f['value'][0,0]))
        self.assertAlmostEqual(f['value'][1,0],.1)
        self.assertTrue(np.isnan(f['revision'][2,0]))
        self.assertAlmostEqual(f['revision'][3,0],.2)

    def test_no_cross_year_or_cross_broker_pair(self):
        d=self.dataset();f,a=forecast_features(d,[self.row('2020-12-28',1,2021),self.row('2020-12-30',2,2022),self.row('2020-12-31',3,2021,'B')])
        self.assertTrue(np.isnan(f['revision']).all())

    def test_current_year_mode_cannot_borrow_next_year_eps_or_revision(self):
        d=self.dataset();rows=[self.row('2020-12-28',1,2021),self.row('2020-12-30',1.5,2021),
            self.row('2021-01-04',2,2022),self.row('2021-01-05',3,2022)]
        f,_=forecast_features(d,rows,current_year_only=True)
        self.assertTrue(np.isnan(f['value'][:4]).all())
        self.assertAlmostEqual(f['value'][6,0],.15)
        self.assertAlmostEqual(f['revision'][6,0],.5)

    def test_negative_broker_is_exposed_for_valuation_quality(self):
        d=self.dataset();f,_=forecast_features(d,[self.row('2020-12-28',-1,2021)],current_year_only=True)
        self.assertEqual(f['nonpositive_brokers'][4,0],1)

    def test_split_crossing_excluded(self):
        d=self.dataset();d['split_factor'][2:,0]=2
        f,a=forecast_features(d,[self.row('2020-12-28',1),self.row('2020-12-30',.6)])
        self.assertTrue(np.isnan(f['revision']).all())
        self.assertEqual(a['split_crossing_pairs_excluded'],1)

    def test_future_rows_do_not_change_past_features(self):
        d=self.dataset();old=[self.row('2020-12-28',1),self.row('2020-12-30',1.2)]
        a,_=forecast_features(d,old);b,_=forecast_features(d,old+[self.row('2021-01-05',2)])
        for k in a:np.testing.assert_equal(a[k][:6],b[k][:6])

    def test_ambiguous_same_day_invalidates_latest(self):
        d=self.dataset();f,a=forecast_features(d,[self.row('2020-12-28',1),self.row('2020-12-30',1.2,doc='x'),self.row('2020-12-30',1.5,doc='y')])
        self.assertTrue(np.isnan(f['value'][3:,0]).all())
        self.assertEqual(a['conflicting_same_day_broker_year'],1)

    def test_broker_gets_one_vote_not_one_vote_per_fiscal_year(self):
        d=self.dataset();rows=[self.row('2020-12-28',1,2021,'A'),self.row('2020-12-28',2,2022,'A'),self.row('2020-12-28',1,2021,'B'),
            self.row('2021-01-04',1.2,2021,'A'),self.row('2021-01-04',2.8,2022,'A'),self.row('2021-01-04',1.1,2021,'B')]
        f,a=forecast_features(d,rows)
        self.assertAlmostEqual(f['revision'][5,0],.2)
        self.assertEqual(f['revision_brokers'][5,0],2)

    def test_small_universe_is_not_bought_wholesale(self):
        scores=np.tile(np.arange(100,dtype=float),(3,1));scores[0,19:]=np.nan;scores[1,30:]=np.nan
        selected,counts=select_fraction(scores)
        np.testing.assert_array_equal(counts,[0,10,20])
        self.assertTrue(np.isnan(selected[0]).all())

    def test_later_indexing_date_delays_availability(self):
        self.assertEqual(public_date('2020-12-28',source_identifier='AP20210104123456'),'2021-01-05')
        d=self.dataset();r=self.row('2020-12-28',1);r['eastmoney_infocode']='AP20210104123456'
        f,_=forecast_features(d,[r]);self.assertTrue(np.isnan(f['value'][:5]).all())
        self.assertAlmostEqual(f['value'][5,0],.1)

    def test_late_indexing_does_not_refresh_stale_report(self):
        dates=np.arange('2020-01-01','2021-01-07',dtype='datetime64[D]').astype(str)
        d={'dates':dates,'symbols':np.asarray(['600001']),'close':np.full((len(dates),1),10.),'split_factor':np.ones((len(dates),1))}
        r=self.row('2020-01-01',1);r['eastmoney_infocode']='AP20201228123456'
        f,a=forecast_features(d,[r]);self.assertTrue(np.isnan(f['value']).all())
        self.assertEqual(a['stale_or_out_of_order_on_arrival'],1)

    def test_late_older_report_cannot_replace_newer_estimate(self):
        d=self.dataset();old=self.row('2020-12-28',1);old['eastmoney_infocode']='AP20210104123456'
        f,a=forecast_features(d,[old,self.row('2020-12-29',1.5),self.row('2020-12-30',2)])
        self.assertAlmostEqual(f['value'][5,0],.2)
        self.assertAlmostEqual(f['revision'][5,0],1/3)
        self.assertEqual(a['stale_or_out_of_order_on_arrival'],1)

if __name__=='__main__':unittest.main()
