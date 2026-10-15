import datetime as dt
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/analytics'))
from cninfo_summary_parser import parse_summary, issuer_identity


class SummaryScopes(unittest.TestCase):
    def test_integer_amounts_and_segment_exclusion(self):
        text='''四、主要会计数据和财务指标
本报告期 上年同期 本报告期比上年同期增减
营业收入（千元） 260,042,490 251,123,714 3.55%
基本每股收益（元/股） 3.52 3.41 3.23%
五、主营业务分析
单位：千元 智能家居 楼宇科技 机器人与自动化 工业技术 其他业务
营业收入 174,340,606 21,625,109 16,620,817 13,137,739 15,282,454
'''
        facts,_=parse_summary(text,dt.date(2026,6,30)); by={r['metric_code']:r for r in facts}
        self.assertEqual(by['revenue']['value'],Decimal('260042490000'))
        self.assertEqual(by['revenue']['yoy'],Decimal('3.55'))
        self.assertEqual(by['eps_basic']['value'],Decimal('3.52'))

    def test_no_issuer_scope_no_facts(self):
        self.assertEqual(parse_summary('单位：千元\n营业收入 174,340,606 21,625,109 16,620,817')[0],[])

    def test_missing_comparative_does_not_shift_percentage(self):
        text='''2、主要会计数据和财务指标
本报告期 上年同期 同比增减
                    124,503,174,115.
营业收入（元） 174,504,335,921.25 40.16%
                               60
归属于上市公司股东的净利润（元） 7,842,698,288.86 6,644,272,049.46 18.04%
3、股东情况
'''
        facts,warning=parse_summary(text)
        self.assertNotIn('revenue',[r['metric_code'] for r in facts])
        self.assertIn('net_profit_parent',[r['metric_code'] for r in facts])
        self.assertTrue(warning)

    def test_inconsistent_yoy_is_not_served(self):
        facts,warnings=parse_summary('主要会计数据\n本报告期 上年同期 同比增减\n营业收入（元） 100 90 123456')
        self.assertEqual(facts[0]['value'],100)
        self.assertIsNone(facts[0]['yoy'])
        self.assertTrue(warnings)

    def test_restatement_extra_columns_are_not_guessed(self):
        facts,warnings=parse_summary('主要会计数据\n本报告期 上年同期 同比增减\n营业收入（元） 100 80 90 11.11')
        self.assertEqual(facts,[])
        self.assertTrue(warnings)

    def test_corrupt_numeric_glyph_does_not_become_truncated_amount(self):
        text='主要财务数据\n单位：元\n本报告期 上年同期 同比增减\n营业收入 24,899,58I,778.93 3I,098,439,150.60 ˉI9.93'
        facts,warnings=parse_summary(text)
        self.assertEqual(facts,[])
        self.assertIn('corrupt_numeric_glyphs',[r['reason'] for r in warnings])

    def test_detached_labels_do_not_shift_revenue_to_profit_row(self):
        text='''2.2 主要财务数据
单位：元
本报告期 上年同期 同比增减
20,441,970,386.00 19,514,431,849.59 4.75
注1
营业收入
2,586,571,906.93 2,718,481,583.08 -4.85
注2
利润总额
归属于上市公司股东的净利润 1,721,467,719.58 1,701,967,773.82 1.15
'''
        facts,_=parse_summary(text)
        self.assertNotIn('revenue',[r['metric_code'] for r in facts])
        self.assertEqual(next(r for r in facts if r['metric_code']=='net_profit_parent')['value'],Decimal('1721467719.58'))

    def test_subsidiary_issuer_is_not_filing_distributor(self):
        text='股票简称 金隅冀东 股票代码 000401\n四、主要会计数据和财务指标\n股东证券代码 601992'
        self.assertTrue(issuer_identity(text,'601992')['conflict'])
        self.assertFalse(issuer_identity(text,'000401')['conflict'])
        self.assertFalse(issuer_identity('股票代码 600196\nH股代码 02196','600196')['conflict'])
        text='中国电信 指中国电信股份有限公司（股票代码 601728）\n股票种类 股票简称 股票代码\nA股 国盾量子 688027'
        self.assertFalse(issuer_identity(text,'688027')['conflict'])
        self.assertEqual(issuer_identity(text,'688027')['explicit_issuer_codes'],['688027'])


if __name__=='__main__': unittest.main()
