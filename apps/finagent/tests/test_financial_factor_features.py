import datetime as dt
import unittest
from scripts.analytics.build_financial_factor_features import ratio_value

class FinancialRatioContractTests(unittest.TestCase):
    def setUp(self):
        self.cutoff=dt.datetime(2026,10,13,14,tzinfo=dt.timezone.utc)
        self.base=dict(asset_id='A',filing_id='F',source_document_id='D',revision_no=1,
            statement_scope='consolidated',quality_status='verified',fact_kind='actual',unit='CNY',currency='CNY',
            period_start=dt.date(2026,1,1),period_end=dt.date(2026,6,30),period_type='year_to_date',
            available_at=self.cutoff-dt.timedelta(days=10),created_at=self.cutoff-dt.timedelta(days=1))
        self.n={**self.base,'metric_code':'net_profit_total','value':'100'}
        self.d={**self.base,'metric_code':'revenue','value':'1000'}
    def test_matched_consolidated_net_margin(self):
        self.assertEqual(ratio_value('reported_net_margin',self.n,self.d,self.cutoff),.1)
    def test_wrong_period_scope_revision_or_units_rejected(self):
        for change in ({'period_end':dt.date(2025,6,30)},{'period_start':dt.date(2026,4,1)},
            {'statement_scope':'parent'},{'unit':'万元'},{'currency':'USD'},{'revision_no':2},
            {'source_document_id':'OTHER'},{'quality_status':'unverified'},{'fact_kind':'forecast'}):
            with self.subTest(change=change):self.assertIsNone(ratio_value('reported_net_margin',{**self.n,**change},self.d,self.cutoff))
    def test_parent_profit_is_not_consolidated_net_margin(self):
        self.assertIsNone(ratio_value('reported_net_margin',{**self.n,'metric_code':'net_profit_parent'},self.d,self.cutoff))
    def test_missing_duration_and_bad_denominator_not_imputed(self):
        for value in ('0','-1','NaN','Infinity',None):
            with self.subTest(value=value):self.assertIsNone(ratio_value('reported_net_margin',self.n,{**self.d,'value':value},self.cutoff))
        self.assertIsNone(ratio_value('reported_net_margin',{**self.n,'period_start':None},{**self.d,'period_start':None},self.cutoff))
    def test_future_publication_rejected_in_both_modes(self):
        for mode in ('system','public'):
            self.assertIsNone(ratio_value('reported_net_margin',{**self.n,'available_at':self.cutoff+dt.timedelta(days=1)},self.d,self.cutoff,mode))
    def test_late_ingestion_public_reconstruction_is_not_system_pit(self):
        later={**self.n,'created_at':self.cutoff+dt.timedelta(days=1)}
        self.assertIsNone(ratio_value('reported_net_margin',later,self.d,self.cutoff,'system'))
        self.assertEqual(ratio_value('reported_net_margin',later,self.d,self.cutoff,'public'),.1)
    def test_balance_sheet_ratio_uses_same_point_date(self):
        n={**self.n,'metric_code':'total_liabilities','period_type':'point_in_time','period_start':None}
        d={**self.d,'metric_code':'total_assets','period_type':'point_in_time','period_start':None}
        self.assertEqual(ratio_value('reported_liabilities_assets',n,d,self.cutoff),.1)
        self.assertIsNone(ratio_value('reported_liabilities_assets',n,{**d,'period_end':dt.date(2025,12,31)},self.cutoff))

if __name__=='__main__':unittest.main()
