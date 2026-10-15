import sys
from pathlib import Path
import unittest
import re
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'MMClaw'),str(ROOT/'scripts/analytics')]
from mmclaw.finance_db_helper import document_excerpt, fts_document_rows
from mmclaw.tool_policy import FINANCE_CHAT_READONLY_TOOLS
from cninfo_summary_parser import parse_summary
import importlib.util
_spec=importlib.util.spec_from_file_location('analytics_common_test',ROOT/'scripts/analytics/common.py')
_analytics_common=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_analytics_common)
_original_common=sys.modules.get('common')
sys.modules['common']=_analytics_common
try:
    from extract_document_events_rules import extract,CompanyResolver
finally:
    if _original_common is None: sys.modules.pop('common',None)
    else: sys.modules['common']=_original_common


class DataToolsRepair(unittest.TestCase):
    def test_original_document_page_and_needle(self):
        result=document_excerpt('封面\f主要会计数据\n营业收入 260042490 251123714\n尾行\f',{'page_number':2,'needle':'营业收入','max_lines':2})
        self.assertEqual(result['page_count'],2)
        self.assertEqual(result['match_line'],2)
        self.assertIn('260042490',result['text'])
        self.assertEqual(result['next_line'],3)
        self.assertEqual(document_excerpt('一\n二',{'start_line':2})['next_line'],None)

    def test_long_line_continuation_does_not_lose_text(self):
        body='a'*15000+'Z\nend'
        first=document_excerpt(body,{})
        second=document_excerpt(body,{'start_line':first['next_line'],'char_offset':first['next_char_offset']})
        self.assertEqual(first['text'][3:]+second['text'].split('\n')[0][3:],body.split('\n')[0])
        self.assertIn('end',second['text'])

    def test_invalid_page_is_not_silently_clamped(self):
        with self.assertRaises(ValueError):document_excerpt('text',{'page_number':2})

    def test_bse_heading_and_growth_row(self):
        text='''2.2 公司主要财务数据
单位：元
本期 上年同期 增减比例
营业收入 1000 900 11.11%
9.12%
基本每股收益 1.0 0.9 11.11%
营业收入增长率 11.11% 9.12% -
3、股本情况'''
        facts,_=parse_summary(text)
        facts={r['metric_code']:r for r in facts}
        self.assertEqual(facts['revenue']['value'],1000)
        self.assertEqual(facts['eps_basic']['value'],1)

    def test_event_does_not_assign_other_company_to_report_owner(self):
        resolver=CompanyResolver([{'symbol':'300014','name':'亿纬锂能'},{'symbol':'300433','name':'蓝思科技'}])
        events,_=extract({'document_id':'example','stock_code':'300014','stock_name':'亿纬锂能',
          'content_text':'亿纬锂能跟踪报告。蓝思科技300433签订合同，获得大单。'},'rules:v2',resolver)
        order=next(e for e in events if e['event_type']=='order_contract')
        self.assertEqual(order['stock_codes'],['300433'])

    def test_explicit_restated_columns_and_wrapped_label(self):
        text='''主要会计数据和财务指标
本报告期 上年同期 本报告期比上年同期增减
调整前 调整后 调整后
营业收入（元） 1000 800 900 11.11%
归属于上市公司股东
    67,395,850.46 61,827,220.33 61,827,220.33 9.01%
的净利润（元）
基本每股收益（元/股） 0.0696 0.0832 0.0640 8.75%'''
        facts,_=parse_summary(text);by={r['metric_code']:r for r in facts}
        self.assertEqual(by['revenue']['prior_value'],'900')
        self.assertEqual(by['revenue']['comparative_basis'],'restated')
        self.assertEqual(str(by['net_profit_parent']['value']),'67395850.46')
        self.assertEqual(str(by['eps_basic']['yoy']),'8.75')

    def test_parent_published_filing_uses_explicit_subsidiary_issuer(self):
        resolver=CompanyResolver([{'symbol':'601992','name':'金隅集团'},{'symbol':'000401','name':'金隅冀东'}])
        events,_=extract({'document_id':'example','source_type':'announcement','stock_code':'601992',
          'stock_name':'金隅集团','content_text':'股票代码 000401\n主要会计数据\n本公司签订合同，获得大单。'},'rules:v2',resolver)
        event=next(e for e in events if e['event_type']=='order_contract')
        self.assertEqual(event['stock_codes'],['000401'])

    def test_wrapped_amount_suffixes_are_never_full_amounts(self):
        text='''主要会计数据和财务指标
本报告期 上年同期 同比增减
           2,188,769,108.8 2,379,965,315.4
营业收入（元）                              -8.03%
                       9               5
归属于上市公司股东的净利润（元） 8283872.94 34269873.48 -75.83%'''
        facts,_=parse_summary(text);by={r['metric_code']:r for r in facts}
        self.assertNotIn('revenue',by)
        self.assertIn('net_profit_parent',by)

    def test_calculator_available_for_chat(self):
        self.assertIn('finance_calculator',FINANCE_CHAT_READONLY_TOOLS)

    def test_full_text_numeric_tail_supports_thousands_and_sign_without_substrings(self):
        for query, accepted, rejected in (
            ('1234567.89', ['金额1,234,567.89元', '金额-1234567.89元'], ['金额91234567.89元', '金额1234567.890元']),
            ('-1234567.89', ['金额-1,234,567.89元'], ['金额1234567.89元']),
            ('+1234567.89', ['金额1,234,567.89元', '金额+1234567.89元'], ['金额-1234567.89元']),
        ):
            with self.subTest(query=query):
                from contextlib import nullcontext
                conn=Mock()
                conn.transaction.return_value=nullcontext()
                conn.execute.return_value.fetchone.return_value={'enable_seqscan':'on'}
                conn.execute.return_value.fetchall.return_value=[]
                _,meta=fts_document_rows(conn,query,[query],[],30,'',10,400,False)
                self.assertTrue(meta['fts_used'])
                params=next(call.args[1] for call in conn.execute.call_args_list if 'WITH bounds AS' in call.args[0])
                for text in accepted:self.assertRegex(text,params['numeric_regex'])
                for text in rejected:self.assertIsNone(re.search(params['numeric_regex'],text))


if __name__=='__main__':unittest.main()
