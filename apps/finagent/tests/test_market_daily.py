"""Cross-market selection, evidence semantics, and one-version delivery."""
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/pipeline'))
import market_daily_inputs as inputs
import daily_ai_hardware_report_v3 as daily
from daily_llm_payload import compact_v3_snapshot
from report_capabilities import analysis_capabilities
import enqueue_daily_report_v3 as outbox


class MarketDailyTests(unittest.TestCase):
    def valuation_fixture(self):
        cutoff=dt.datetime(2026,10,16,8,50,tzinfo=dt.timezone(dt.timedelta(hours=8)))
        observations=[];assets=[]
        for i in range(12):
            code=f'{600100+i}'
            assets.append({'stock_code':code,'name':f'公司{i}','industry':'工业金属' if i<11 else '银行Ⅱ',
                'close':10 if i in (0,11) else 20,'quality_status':'verified','price_basis':'raw',
                'available_at':cutoff-dt.timedelta(days=1),'market_date':cutoff.date()-dt.timedelta(days=1)})
            for broker in ('甲机构','乙机构','丙机构'):
                observations.append({'stock_code':code,'institution':broker,'metric':'eps','forecast_year':2026,
                    'report_date':cutoff.date()-dt.timedelta(days=1),'known_at':cutoff-dt.timedelta(hours=1),
                    'observed_at':cutoff-dt.timedelta(hours=1),'source_basis':'observed_api_current_year',
                    'unit':'CNY/share','value':1,'document_id':code+broker,'evidence':{}})
        conn=mock.Mock()
        def execute(sql,params):
            rows=assets if 'WITH industry AS' in sql else observations if 'UNION ALL' in sql else []
            cursor=mock.MagicMock();cursor.fetchall.return_value=rows;cursor.__iter__.return_value=iter(rows)
            return cursor
        conn.execute.side_effect=execute
        return cutoff,observations,assets,conn

    def test_discount_requires_comparable_peers_and_financials_are_not_forced_into_pe(self):
        cutoff,obs,assets,conn=self.valuation_fixture()
        rows,coverage=inputs.load_valuation_screen(conn,cutoff,cutoff.date()-dt.timedelta(days=1),inputs.load_config())
        metal=next(r for r in rows if r['stock_code']=='600100')
        bank=next(r for r in rows if r['stock_code']=='600111')
        self.assertEqual(metal['peer_discount_percent'],50)
        self.assertTrue(metal['supported_discount'])
        self.assertFalse(metal['prediction_eligible'])
        self.assertFalse(bank['supported_discount']);self.assertIsNone(bank['peer_pe'])
        self.assertEqual(coverage['covered_stocks'],12)

    def test_future_eps_suspended_assets_and_single_source_prices_do_not_qualify(self):
        cutoff,obs,assets,conn=self.valuation_fixture()
        assets[0]['is_suspended']=True
        assets[1]['quality_status']='unverified'
        # A future update cannot replace the last observable EPS or change PE.
        obs.append({**obs[3],'report_date':cutoff.date(),'value':100,
                    'known_at':cutoff+dt.timedelta(hours=1),'observed_at':cutoff+dt.timedelta(hours=1)})
        rows,_=inputs.load_valuation_screen(conn,cutoff,cutoff.date()-dt.timedelta(days=1),inputs.load_config())
        self.assertNotIn('600100',{r['stock_code'] for r in rows})
        row=next(r for r in rows if r['stock_code']=='600101')
        self.assertEqual(row['eps_median'],1);self.assertFalse(row['supported_discount'])
        self.assertIn('收盘价仍为单来源行情',row['gaps'])

    def test_industry_diversity_does_not_depend_on_ai_keywords(self):
        rows=[{'stock_code':str(i),'industry':sector,'name':'company'} for i,sector in enumerate(
            ['半导体']*10+['银行Ⅱ','医药','煤炭','汽车','食品','电力'])]
        selected=inputs.diversify(rows,'industry',6,2)
        self.assertEqual(len({r['industry'] for r in selected}),6)
        self.assertIn('煤炭',{r['industry'] for r in selected})
        self.assertEqual(rows[0]['stock_code'],'0')

    def test_editorial_selection_preserves_candidate_membership_and_company_facts(self):
        cfg=inputs.load_config();cfg['watchlist_limit']=4
        value=[{'stock_code':'600111','name':'资源甲','industry':'金属','forward_pe':8,'gaps':['peer gap']}]
        pool=[{'stock_code':'600222','name':'医药乙','industry':'医药','state':'watching','change':'retained',
               'thesis':['利润增长'],'financial_evidence':[{'metric':'营收同比','value':12,'unit':'%'}]},
              {'stock_code':'600333','name':'退出丙','state':'removed','industry':'电力'}]
        before=copy.deepcopy(pool)
        rows=inputs.report_watchlist(value,pool,[{'industry':'医药'}],cfg)
        self.assertEqual(pool,before)
        self.assertEqual({r['stock_code'] for r in rows},{'600111','600222'})
        self.assertIsNone(rows[0]['reason']['pool_state'])
        self.assertEqual(rows[1]['reason']['pool_state'],'watching')
        self.assertEqual(rows[1]['research']['actual_financials'][0]['value'],12)

    def test_financial_enrichment_does_not_restore_the_old_ai_watchlist(self):
        import scripts.analytics.research_snapshot as shared
        cutoff=dt.datetime(2026,10,16,0,50,tzinfo=dt.timezone.utc)
        selected=[{'stock_code':'600100','research':{'thesis':['保留研究理由']}}]
        values=[{'stock_code':'600100','forward_pe':10}]
        compact={'rows':[{'stock_code':'600100','as_of_date':'2026-10-15',
                          'actual_financials':[{'metric_code':'revenue','value':100,'source_document_id':'filing'}],
                          'financial_ratios':[],'market':{'close':10}}],
                 'watchlist':[{'stock_code':'000001'}]}
        with mock.patch.object(shared,'load_snapshot',return_value={}) as load, \
             mock.patch.object(shared,'compact_research',return_value=compact):
            inputs.enrich_financials(None,cutoff,dt.date(2026,10,15),selected,values)
        self.assertEqual(load.call_args.args[1],cutoff)
        self.assertEqual(load.call_args.kwargs['codes'],['600100'])
        self.assertEqual([r['stock_code'] for r in selected],['600100'])
        self.assertEqual(selected[0]['research']['thesis'],['保留研究理由'])
        self.assertEqual(values[0]['reported_financials'][0]['source_document_id'],'filing')

    def test_full_market_does_not_automatically_authorize_buys(self):
        snapshot={'report_scope':'all_a_share','hard_gate_passed':True,
                  'shared_research':{'watchlist':[{'stock_code':'600000'}]},
                  'evidence':[{'evidence_id':'V1','claim_type':'calculation','metric_id':'valuation_screen',
                    'value':{'stock_code':'600000','forward_pe':8}}]}
        self.assertTrue(analysis_capabilities(snapshot)['evidence_analysis'])
        self.assertFalse(analysis_capabilities(snapshot)['stock_recommendations'])

    def test_sector_attention_is_not_numeric_forecast_permission(self):
        snapshot={'report_date':'2026-10-16','decision_cutoff':'2026-10-16T08:50:00+08:00',
            'market_date':'2026-10-15','report_scope':'all_a_share','hard_gate_passed':True,
            'shared_research':{'watchlist':[]},'evidence':[{'evidence_id':'VAL0001',
                'claim_type':'calculation','metric_id':'valuation_screen','value':{
                    'stock_code':'600000','forward_pe':8,'peer_discount_percent':20,'prediction_eligible':False}}]}
        claim={'claim_id':'C1','section_name':'低估候选与估值观察','claim_type':'interpretation',
               'claim_text':'预测市盈率8倍，先核对盈利兑现。','evidence_ids':['VAL0001']}
        self.assertFalse(daily.validate_claims([copy.deepcopy(claim)],snapshot)[1])
        claim['claim_text']='预期收益20%。'
        self.assertTrue(daily.validate_claims([claim],snapshot)[1])

    def test_model_payload_keeps_value_heat_coverage_and_source_diversity(self):
        evidence=[{'evidence_id':f'{prefix}{i}','metric_id':metric,'claim_type':'calculation',
                   'value':{'name':'test','data':'x'*1000}} for prefix,metric,n in (
                       ('VAL','valuation_screen',8),('HOT','sector_research_attention',8),('COVER','market_coverage',1)) for i in range(n)]
        evidence.extend({'evidence_id':f'DOC{i}','metric_id':'published_document','claim_type':'document_claim',
                         'value':{'source_name':['news_a','news_b','filings'][i%3],'parts':['x'*1100]*5}} for i in range(40))
        payload=compact_v3_snapshot({'evidence':evidence})
        ids={r['evidence_id'] for r in payload['evidence']}
        self.assertTrue({'VAL0','VAL1','HOT0','HOT1','COVER0','DOC0','DOC1','DOC2'}<=ids)
        self.assertLess(len(json.dumps(payload,ensure_ascii=False)),91000)

    def test_reader_sources_have_meaning_and_report_has_no_brief_section(self):
        from mmclaw.presentation import render_finance_text
        snap={'evidence':[{'evidence_id':'VAL0001','metric_id':'valuation_screen','claim_type':'calculation',
                          'value':{'name':'示例公司'},'as_of_date':'2026-10-15'}]}
        text=render_finance_text('估值观察。[evidence: VAL0001]',evidence=snap)
        self.assertNotIn('VAL1',text);self.assertIn('机构盈利预测与估值筛选',text)
        snap['evidence'].append({'evidence_id':'POOL0001','metric_id':'frozen_research_watchlist',
            'claim_type':'model_signal','value':{'name':'示例公司'},'as_of_date':'2026-10-15'})
        self.assertIn('公司研究档案《示例公司》',render_finance_text('依据。[evidence: POOL0001]',evidence=snap))
        self.assertNotIn('省流版',daily.SECTIONS)
        rendered=daily.render_report(dt.date(2026,10,16),dt.datetime.now(dt.timezone.utc),dt.date(2026,10,15),[], 'published',[])
        self.assertIn('# 全市场每日投资建议',rendered)

    def test_outbox_only_full_chunks_preserves_existing_dedup_keys(self):
        conn=mock.MagicMock();conn.__enter__.return_value=conn
        select=mock.Mock();select.fetchone.return_value={'report_run_id':'run','status':'published',
            'output_path':'/preview.md','validation':{'shadow':False}}
        insert=mock.Mock(rowcount=1);conn.execute.side_effect=[select,insert,insert]
        with mock.patch.object(outbox,'connect',return_value=conn),mock.patch.object(outbox,'qq_groups',return_value=['group']), \
             mock.patch.object(outbox,'render_report_file',return_value='text'),mock.patch.object(outbox,'split_text',return_value=['part1','part2']), \
             mock.patch.object(sys,'argv',['enqueue','--date','2026-10-16']):
            self.assertEqual(outbox.main(),0)
        params=conn.execute.call_args_list[0].args[1]
        self.assertEqual(params[0],['ai_hardware_daily_v3'])
        inserts=[call.args[1] for call in conn.execute.call_args_list[1:]]
        self.assertEqual([p[1] for p in inserts],['daily_report','daily_report'])
        self.assertEqual([p[2] for p in inserts],['ai-hardware-v3:2026-10-16:daily_report:1','ai-hardware-v3:2026-10-16:daily_report:2'])


if __name__=='__main__':unittest.main()
