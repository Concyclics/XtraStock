import contextlib
import datetime as dt
import io
import json
import unittest
from mmclaw.finance_db_helper import display_timestamps,emit_v3,v3_request_context,run_v3_time_context
from mmclaw.tool_control import ToolLoopController

class TimePresentationTests(unittest.TestCase):
    def test_midnight_conversion_preserves_instant_and_period_dates(self):
        value={'updated_at':'2026-10-13 17:09:00+00:00','period_end':'2026-06-30',
               'nested':[dt.datetime(2026,10,13,16,tzinfo=dt.timezone.utc)],'naive':'2026-10-13 17:09:00',
               'source_text':'原文: 2026-10-13T17:09:00Z'}
        actual=display_timestamps(value)
        self.assertEqual(actual['updated_at'],'2026-10-14T01:09:00+08:00')
        self.assertEqual(actual['nested'],['2026-10-14T00:00:00+08:00'])
        for k in ['period_end','naive','source_text']:self.assertEqual(actual[k],value[k])
        self.assertEqual(dt.datetime.fromisoformat(actual['updated_at']),dt.datetime.fromisoformat(value['updated_at']))

    def test_cutoff_date_matches_beijing_but_instant_is_unchanged(self):
        raw='2026-10-13T17:09:00Z'
        _,cutoff=v3_request_context({'decision_cutoff':raw})
        self.assertEqual(cutoff.date(),dt.date(2026,10,14))
        self.assertEqual(cutoff,dt.datetime.fromisoformat(raw))

    def test_explicit_clock_timezone_is_honored(self):
        out=io.StringIO()
        with contextlib.redirect_stdout(out):run_v3_time_context('',{'timezone':'UTC'})
        p=json.loads(out.getvalue());self.assertEqual(p['meta']['display_timezone'],'UTC')
        self.assertTrue(p['rows'][0]['current_time'].endswith('+00:00'))

class CacheEquivalenceTests(unittest.TestCase):
    def test_default_clock_and_code_alias_share_one_call(self):
        signature=ToolLoopController.call_signature
        self.assertEqual(signature('finance_time_context',{}),signature('finance_time_context',{'timezone':'Asia/Shanghai'}))
        self.assertEqual(signature('finance_graph_analysis',{'stock_code':'300308'}),signature('finance_graph_analysis',{'stock_codes':['300308']}))
        self.assertEqual(signature('finance_market_series_batch',{'stock_codes':['300308']}),signature('finance_market_series_batch',{'stock_codes':['300308'],'price_basis':'total_return'}))

    def test_distinct_cutoff_filters_and_price_basis_are_never_merged(self):
        signature=ToolLoopController.call_signature
        base={'stock_codes':['300308'],'decision_cutoff':'2026-10-13T12:00:00+08:00'}
        for change in [{'decision_cutoff':'2026-10-14T12:00:00+08:00'},{'price_basis':'raw_unadjusted'},{'start_date':'2026-10-01'}]:
            self.assertNotEqual(signature('finance_market_series_batch',base),signature('finance_market_series_batch',{**base,**change}))


class GraphBudgetTests(unittest.TestCase):
    def test_graph_row_and_evidence_cannot_be_compacted_independently(self):
        from mmclaw.finance_db_helper import bound_graph_pairs
        rows=[{'id':i,'text':'原文'*400} for i in range(30)]
        evidence=[{'id':i,'text':'引文'*400} for i in range(30)]
        evidence.append({'id':'path','text':'path evidence'})
        meta=bound_graph_pairs(rows,evidence,budget=5000)
        self.assertTrue(rows);self.assertLess(len(rows),30)
        self.assertEqual([r['id'] for r in rows],[e['id'] for e in evidence[:-1]])
        self.assertEqual(evidence[-1]['id'],'path')
        self.assertEqual(meta['rows_omitted_from_output'],30-len(rows))
