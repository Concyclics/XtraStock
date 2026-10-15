import datetime as dt
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from cninfo_statement_parser import parse_statements, parse_annual_prospectus


BALANCE='''合并资产负债表
2026 年 6 月 30 日
单位：万元 币种：人民币
项目 2026 年 6 月 30 日 2025 年 12 月 31 日
流动资产合计 40.00 30.00
非流动资产合计 60.00 50.00
资产总计 100.00 80.00
流动负债合计 10.00 15.00
非流动负债合计 30.00 25.00
负债合计 40.00 40.00
所有者权益（或股东权益）：
实收资本（或股本） 七、54 35.00 25.00
归属于母公司所有者权益
  50.00 30.00
少数股东权益 10.00 10.00
所有者权益（或股东权
  60.00 40.00
益）合计
公司负责人：某某
母公司资产负债表
资产总计 999.00 999.00
'''


class StatementParsingTests(unittest.TestCase):
    def test_units_periods_scope_and_equations(self):
        rows,checks=parse_statements(BALANCE,dt.date(2026,6,30))
        values={(r['metric_code'],r['period_end']):r for r in rows}
        current=values[('total_assets',dt.date(2026,6,30))]
        self.assertEqual(current['value'],Decimal('1000000.00'))
        self.assertEqual(current['period_type'],'point_in_time')
        self.assertIsNone(current['period_start'])
        self.assertEqual(values[('total_assets',dt.date(2025,12,31))]['value'],Decimal('800000.00'))
        self.assertEqual(len(checks),8)
        self.assertEqual(values[('total_equity',dt.date(2026,6,30))]['value'],Decimal('600000.00'))

    def test_unbalanced_statement_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'identity failed'):
            parse_statements(BALANCE.replace('资产总计 100.00','资产总计 110.00'),dt.date(2026,6,30))

    def test_missing_unit_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'unit'):
            parse_statements(BALANCE.replace('单位：万元',''),dt.date(2026,6,30))

    def test_wrong_year_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'years'):
            parse_statements(BALANCE,dt.date(2024,6,30))

    def test_note_numbers_and_parent_statement_are_not_financial_values(self):
        rows,_=parse_statements(BALANCE,dt.date(2026,6,30))
        self.assertNotIn(Decimal('540000'),[r['value'] for r in rows])
        self.assertNotIn(Decimal('9990000'),[r['value'] for r in rows])

    def test_summary_actual_comparatives_and_missing_yoy(self):
        text='''2.2 主要财务数据
单位：元 币种：人民币
本报告期末 上年度末
总资产 2,000.00 1,500.00 33.33
本报告期 上年同期
营业收入 300.00 200.00 50.00
归属于上市公司股东的净利润 30.00 -20.00 不适用
经营活动产生的现金流量净额 40.00 10.00 300.00
基本每股收益（元/股） 1.5000 -1.0000 不适用
2.3 前十名股东
'''
        rows,_=parse_statements(text,dt.date(2026,6,30))
        values={(r['metric_code'],r['period_end']):r for r in rows}
        self.assertEqual(values[('revenue',dt.date(2025,6,30))]['value'],Decimal(200))
        self.assertIsNone(values[('net_profit_parent',dt.date(2026,6,30))]['yoy'])
        self.assertEqual(values[('eps_basic',dt.date(2026,6,30))]['unit'],'CNY/share')
        self.assertEqual(values[('total_assets',dt.date(2026,6,30))]['period_type'],'point_in_time')


class AnnualProspectusTests(unittest.TestCase):
    SAMPLE = """（一）合并资产负债表
单位：元
项目 2025 年 12 月 31 日 2024 年 12 月 31 日 2023 年 12 月 31 日
资产总计 100.00 80.00 60.00
负债合计 40.00 30.00 20.00
所有者权益合计 60.00 50.00 40.00
交易性金融资产 10.00 - 5.00
（二）合并利润表
单位：元
项目 2025 年度 2024 年度 2023 年度
营业收入 200.00 100.00 80.00
净利润 20.00 -10.00 -20.00
1.归属于母公司所有者的净利
  15.00 -8.00 -16.00
润
少数股东损益 5.00 -2.00 -4.00
（三）合并现金流量表
单位：元
项目 2025 年度 2024 年度 2023 年度
经营活动产生的现金流量 40.00 30.00 20.00
投资活动产生的现金流量 -30.00 -20.00 -10.00
筹资活动产生的现金流量 20.00 10.00 5.00
汇率变动对现金及现金 -1.00 -2.00 -3.00
现金及现金等价物净增 29.00 18.00 12.00
期初/年初现金及现金 10.00 8.00 6.00
期末/年末现金及现金 39.00 26.00 18.00
二、会计师事务所的审计意见
营业收入 999.00 999.00 999.00
"""
    def test_three_years_dashes_wrapped_labels_and_section_boundaries(self):
        rows, checks = parse_annual_prospectus(self.SAMPLE)
        values = {(r['metric_code'],r['period_end'].year):r for r in rows}
        self.assertEqual(values[('revenue',2023)]['value'],Decimal('80'))
        self.assertEqual(values[('net_profit_parent',2025)]['value'],Decimal('15'))
        self.assertNotIn(('trading_financial_assets',2024),values)
        self.assertEqual(values[('trading_financial_assets',2023)]['value'],Decimal('5'))
        self.assertEqual(len(checks),12)
        self.assertEqual(values[('revenue',2024)]['period_type'],'annual')
        self.assertIsNone(values[('revenue',2025)]['yoy'])
    def test_annual_inconsistent_balance_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'identity failed'):
            parse_annual_prospectus(self.SAMPLE.replace('资产总计 100.00','资产总计 101.00'))
    def test_unrecognized_annual_columns_fail_closed(self):
        with self.assertRaisesRegex(ValueError,'columns'):
            parse_annual_prospectus(self.SAMPLE.replace('2024 年','2022 年'))


if __name__=='__main__':
    unittest.main()
