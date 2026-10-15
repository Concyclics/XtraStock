import contextlib
import io
import json
import queue
import threading
import unittest
from unittest import mock

from mmclaw.agentic_loop import ResearchLoop
from mmclaw.evidence import EvidenceStore, with_quality_evidence
from mmclaw.kernel import MMClaw
from mmclaw.memory import StatelessMemory
from mmclaw.tool_control import ToolLoopController
from mmclaw.tool_schemas import get_native_tool_schemas
from mmclaw.tools import FinanceDBTool


E1 = "a" * 32 + ":E0001"
E2 = "a" * 32 + ":E0002"
CONFIG = {"tool_profile":"finance_chat_readonly", "tool_calling_mode":"json"}


class AttributedDocumentValuesTests(unittest.TestCase):
    def setUp(self):
        self.store = EvidenceStore("a"*32, "分析这些板块的估值和走势")
        self.store.items[E1] = {"evidence_id":E1,"claim_type":"document_claim","unit":"document",
            "value":{"title":"行业周报", "snippet":"截至10月9日，PE分位数为11.9%，动力煤998元/吨，电子布13-13.3元/米。7628布价格上涨0.74%。"}}

    def test_cited_document_percentages_and_commodity_units_are_valid(self):
        for sentence in ["研报称PE分位数为11.9%。", "研报称动力煤998元/吨。",
                         "研报称电子布价格13.3元/米。", "研报称截至10月9日，7628布上涨0.74%。"]:
            answer = sentence + f"[{E1}]"
            self.assertTrue(self.store.validate(answer)["passed"],self.store.validate(answer))

    def test_quote_does_not_authorize_changed_number_unit_or_actual_valuation(self):
        for sentence in ["研报称动力煤999元/吨。", "研报称动力煤998元/米。",
                         "股价998元。", "PE分位数为11.9%。", "研报称上涨74%。"]:
            self.assertFalse(self.store.validate(sentence+f"[{E1}]")["passed"],sentence)

    def test_unrelated_paragraph_cannot_borrow_report_number(self):
        text=f"研报称上涨0.74%。[{E1}]\n\n公司净利润上涨0.74%。"
        self.assertFalse(self.store.validate(text)["passed"])

    def test_one_invalid_paragraph_does_not_erase_valid_analysis(self):
        good=f"研报称PE分位数为11.9%。[{E1}]"
        result=self.store.supported_answer(good+f"\n\n股价999元。[{E1}]")
        self.assertIn(good,result)
        self.assertNotIn("999",result)
        self.assertNotIn("未通过证据校验",result)

    def test_invented_prediction_still_fails(self):
        self.assertFalse(self.store.validate(f"模型目标价998元。[{E1}]")["passed"])

    def test_snapshot_horizons_and_prices_are_typed_without_authorizing_pe_as_yuan(self):
        self.store.items[E2]={"claim_type":"calculation","metric_id":"finance_market_screen",
            "value":{"close":"12.34","pe_ttm":"11.5","ret_20d":"-0.1370"}}
        answer=f"收盘12.34元，近20个交易日变动-13.70%，20日新低。[{E2}]"
        self.assertTrue(self.store.validate(answer)["passed"],self.store.validate(answer))
        self.assertFalse(self.store.validate(f"收盘11.5元。[{E2}]")["passed"])

    def test_decline_magnitude_is_not_mistaken_for_opposite_return(self):
        self.store.items[E2]={"claim_type":"calculation","metric_id":"finance_market_screen",
            "value":{"ret_20d":"-0.1370"}}
        self.assertTrue(self.store.validate(f"近20日下跌13.70%。[{E2}]")["passed"])
        self.assertFalse(self.store.validate(f"近20日上涨13.70%。[{E2}]")["passed"])

    def test_markdown_list_number_is_not_a_financial_claim(self):
        self.assertTrue(self.store.validate(f"**2）研报观点**\n研报称PE分位数11.9%。[{E1}]")["passed"])
        self.assertFalse(self.store.validate(f"净利润为2亿元。[{E1}]")["passed"])

    def test_eps_yuan_per_share_is_not_company_revenue(self):
        self.store.items[E2]={"claim_type":"actual","metric_id":"eps_basic","unit":"CNY/share",
            "value":{"value":"0.5102","unit":"CNY/share"}}
        self.assertTrue(self.store.validate(f"每股收益0.5102元。[{E2}]")["passed"])
        self.assertFalse(self.store.validate(f"营业收入0.5102元。[{E2}]")["passed"])

    def test_grouped_shorthand_resolves_only_with_one_known_prefix(self):
        self.store.items[E2]=dict(self.store.items[E1],evidence_id=E2)
        expanded=self.store.normalize_citations(f"研报称PE分位数11.9%。[{E1}、E0002]")
        self.assertIn(E2,expanded)
        self.assertTrue(self.store.validate(expanded)["passed"])
        missing=self.store.normalize_citations(f"研报称PE分位数11.9%。[{E1}、E0099]")
        self.assertFalse(self.store.validate(missing)["passed"])
        self.assertEqual(self.store.normalize_citations("E0002"),"E0002")
        unbracketed=self.store.normalize_citations(f"研报称PE分位数11.9%。{E1}、E0002")
        self.assertTrue(self.store.validate(unbracketed)["passed"])
        self.assertFalse(self.store.supported_answer(f"股价999元。[{E1}]",fallback_to_facts=False))

    def test_date_ranges_and_conjoined_declines_are_not_extra_claims(self):
        self.store.items[E2]={"claim_type":"calculation","metric_id":"finance_market_screen",
            "value":{"ret_20d":"-0.1370", "other_return":"-0.5678"}}
        for text in ["2026-09-17至10-12，两者分别下跌13.70%和56.78%。",
                     "9月17日至10月12日，两者分别下跌13.70%和56.78%。",
                     "日期为2026-09-22或09-23，两者分别下降13.70%和56.78%。"]:
            self.assertTrue(self.store.validate(text+f"[{E2}]")["passed"],text)
        self.assertFalse(self.store.validate(f"上涨13.70%和56.78%。[{E2}]")["passed"])

    def test_shorthand_dates_do_not_mask_financial_amounts(self):
        for dates in ("10月8日至12日", "2026-10-08、09、12"):
            self.assertTrue(self.store.validate(f"{dates}研报称PE分位数为11.9%。[{E1}]")["passed"])
        for text in ("2026-10-08、09、12%", "10月8日至12亿元", "2026-10-08、09、12.7%"):
            self.assertFalse(self.store.validate(text+f"[{E1}]")["passed"], text)

    def test_factor_horizon_is_metadata_only_when_cited(self):
        self.store.items[E2]={"claim_type":"model_signal", "metric_id":"graph_event_impulse_30d",
                              "value":{"factor_name":"graph_event_impulse_30d", "raw_value":6.705}}
        self.assertTrue(self.store.validate(f"30日事件脉冲属于研究信号。[{E2}]")["passed"])
        self.assertFalse(self.store.validate(f"30日事件脉冲属于研究信号。[{E1}]")["passed"])
        self.assertFalse(self.store.validate(f"信号收益30%。[{E2}]")["passed"])

    def test_query_coverage_is_citable_without_authorizing_returns(self):
        payload=json.dumps({"meta":{"request_id":"b"*32,"status":"ok","coverage_pct":31.8181818,
                    "returned_factor_stock_pairs":21,"expected_factor_stock_pairs":66,"secret":"omit"},"evidence":[]})
        result=with_quality_evidence('finance_factor_analysis',payload)
        self.store.add_tool_result('finance_factor_analysis',result)
        key='b'*32+':E0000'
        self.assertNotIn('secret',self.store.items[key]['value'])
        self.assertTrue(self.store.validate(f"本次查询返回21个因子，应有66个，覆盖率31.82%。[{key}]")["passed"])
        self.assertFalse(self.store.validate(f"收益31.82%。[{key}]")["passed"])
        self.assertFalse(self.store.validate(f"营业收入21元。[{key}]")["passed"])
        failed=payload.replace('"ok"','"error"')
        self.assertEqual(with_quality_evidence('finance_factor_analysis',failed),failed)

    def test_return_calculation_has_yuan_endpoints_but_percent_result(self):
        self.store.items[E2]={"claim_type":"calculation", "metric_id":"period_return_pct_raw_unadjusted",
            "unit":"percentage_points", "value":{"stock_code":"300308", "start_close":"1108.00",
                                                    "end_close":"724.47", "return_pct":-34.614621}}
        self.assertTrue(self.store.validate(f"收盘从1108.00元降至724.47元，区间下跌34.61%。[{E2}]")["passed"])
        self.assertFalse(self.store.validate(f"股价34.61元。[{E2}]")["passed"])

    def test_report_excerpt_in_graph_is_quotable_only_with_attribution(self):
        self.store.items[E2]={"claim_type":"graph_association", "source_document_id":"doc",
            "value":{"claim_class":"extracted_claim", "source_system":"research.reports",
                     "evidence_text":"归母净利润同比减少20.47%。"}}
        self.assertTrue(self.store.validate(f"研报转述归母净利润下降20.47%。[{E2}]")["passed"])
        self.assertFalse(self.store.validate(f"研报转述归母净利润上涨20.47%。[{E2}]")["passed"])
        self.assertFalse(self.store.validate(f"归母净利润下降20.47%。[{E2}]")["passed"])
        self.store.items[E2]['value']['source_system']='unrelated'
        self.assertFalse(self.store.validate(f"研报转述归母净利润下降20.47%。[{E2}]")["passed"])


class ScreenOutputBudgetTests(unittest.TestCase):
    def test_large_legacy_screen_retains_ranked_rows_and_omission_counts(self):
        rows=[{"stock_code":f"60000{i}","stock_name":"示例公司","pe_ttm":10+i,
               "description":"数据"*250} for i in range(8)]
        with mock.patch.object(FinanceDBTool,"MAX_OUTPUT_CHARS",2200):
            bounded=FinanceDBTool._bound_child_output(json.dumps({"meta":{"strict_candidate_count":80},"rows":rows}))
            data=json.loads(bounded)
            self.assertGreater(len(data['rows']),0)
            self.assertLess(len(data['rows']),8)
            self.assertEqual(data['rows'],rows[:len(data['rows'])])
            self.assertEqual(data['meta']['rows_omitted_from_output'],8-len(data['rows']))
            self.assertEqual(data['meta']['strict_candidate_count'],80)
            wrapped=json.loads(FinanceDBTool._v3_wrap(bounded,"finance_market_screen","calculation"))
            self.assertGreater(len(wrapped['evidence']),0)
            self.assertEqual(wrapped['evidence'][0]['value'],rows[0])
            self.assertEqual(wrapped['meta']['status'],'ok')


if __name__=='__main__':unittest.main()
