import datetime as dt
import json
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'MMClaw'))
from scripts.analytics.data_catalog import load_data_catalog
from scripts.analytics.research_asset_registry import public_experiment_contract, load_experiment_results
from mmclaw import finance_db_helper as helper
from mmclaw.tools import FinanceDBTool


class RegistryAlignmentTests(unittest.TestCase):
    def test_registered_file_without_measurement_is_not_missing_or_approved(self):
        row={'dataset_key':'research.experiments','catalog_available_at':dt.datetime.now(dt.timezone.utc),
             'quality_status':'unknown','measurement_status':'not_measured'}
        with patch.object(helper,'connect',return_value=MagicMock()), patch(
            'scripts.analytics.data_catalog.load_data_catalog',return_value=[row]),patch.object(helper,'emit_v3') as emit:
            helper.run_v3_data_status(str(ROOT),{'scope':'catalog','dataset_keys':['research.experiments']})
        self.assertEqual(emit.call_args.kwargs['status'],'ok')
        self.assertIsNone(emit.call_args.kwargs['extra_meta']['hard_gate_passed'])
        self.assertIsNone(emit.call_args.kwargs['coverage_pct'])

    def test_catalog_bounds_and_paths_are_not_exposed(self):
        conn=MagicMock();conn.execute.return_value.fetchall.return_value=[]
        load_data_catalog(conn,dt.datetime.now(dt.timezone.utc),['research.experiments'],1000)
        sql,params=conn.execute.call_args.args
        self.assertEqual(params['limit'],100)
        self.assertIn("r.storage_kind='sql' THEN r.locator ELSE NULL",sql)
        self.assertIn('r.updated_at<=',sql)
        self.assertNotIn('count(*)',sql.lower())

    def test_catalog_pages_remain_small_enough_to_keep_citable_rows(self):
        now=dt.datetime.now(dt.timezone.utc)
        rows=[{'dataset_key':str(i),'catalog_available_at':now} for i in range(10)]
        with patch.object(helper,'connect',return_value=MagicMock()),patch(
            'scripts.analytics.data_catalog.load_data_catalog',return_value=rows) as read,patch.object(helper,'emit_v3') as emit:
            helper.run_v3_data_status(str(ROOT),{'scope':'catalog','limit':100,'catalog_offset':10})
        self.assertEqual(read.call_args.args[3],10)
        self.assertEqual(read.call_args.kwargs['offset'],10)
        self.assertEqual(emit.call_args.kwargs['extra_meta']['catalog_next_offset'],20)

    def test_client_forwards_catalog_scope(self):
        with patch.object(FinanceDBTool,'_v3_child',return_value='{}') as child:
            FinanceDBTool.data_status(scope='catalog')
        self.assertEqual(child.call_args.args[1]['scope'],'catalog')

    def test_contract_preserves_empty_training_note_but_omits_paths_and_hashes(self):
        contract={'contract_version':1,'result_artifacts':{'secret/path':'abcd'},
                  'metric_units':{'cost':'bps per side'},'gate_status':'failed',
                  'year_notes':{'2021':'empty training'},'training_status_by_year':{'2021':'fixed_rule_diagnostic'}}
        conn=MagicMock();conn.execute.return_value.fetchall.return_value=[{
            'project_key':'trial','artifact_id':'id','row_number':0,'available_at':dt.datetime.now(dt.timezone.utc),
            'payload':{'year':2021,'cost':15,'net_return':.02},'result_contract':contract}]
        rows=load_experiment_results(conn,dt.datetime.now(dt.timezone.utc),offset=20)
        self.assertEqual(rows[0]['training_status'],'fixed_rule_diagnostic')
        self.assertEqual(rows[0]['metric_units'],{'cost':'bps per side'})
        self.assertFalse(rows[0]['prediction_eligible'])
        self.assertNotIn('secret/path',json.dumps(public_experiment_contract(contract)))
        self.assertEqual(conn.execute.call_args.args[1][-2],20)
        self.assertIn('a.content_sha256',conn.execute.call_args.args[0])

    def test_invalid_pagination_and_filters_are_rejected(self):
        for offset in (-1,10001):
            with self.assertRaises(ValueError):load_experiment_results(MagicMock(),None,offset=offset)
        with self.assertRaises(ValueError):load_experiment_results(MagicMock(),None,filters={'arbitrary':'x'})

    def test_agent_experiment_page_keeps_evidence_within_budget(self):
        from scripts.analytics.research_intelligence import load_intelligence
        with patch('scripts.analytics.research_asset_registry.load_registry_summary',return_value={}) as catalog,patch(
            'scripts.analytics.research_asset_registry.load_experiment_results',return_value=[{}]*5) as results:
            state=load_intelligence(MagicMock(),dt.datetime.now(dt.timezone.utc),limit=20,scope='experiments',experiment_project='fixture',experiment_offset=5)
        self.assertEqual(results.call_args.args[3],5)
        self.assertEqual(catalog.call_args.args[3],'fixture')
        self.assertEqual(state['experiments']['next_offset'],10)

    @unittest.skipUnless(os.getenv('FINAGENT_TEST_DB')=='1','requires temporary SQL tables')
    def test_new_result_content_is_excluded_until_contract_matches(self):
        from scripts.analytics.common import connect,jsonb
        import uuid
        cutoff=dt.datetime.now(dt.timezone.utc)
        with connect(autocommit=False) as conn:
            try:
                conn.execute("SET LOCAL statement_timeout='5s'")
                conn.execute('''CREATE TEMP TABLE experiment_artifacts(
                  artifact_id uuid,relative_path text,project_key text,registered_at timestamptz,
                  artifact_kind text,payload_status text,content_sha256 text) ON COMMIT DROP''')
                conn.execute('CREATE TEMP TABLE artifact_records(artifact_id uuid,row_number int,payload jsonb) ON COMMIT DROP')
                for name,age,sha,payload in [
                    ('COSTED_RESULTS.csv',30,'a'*64,{'year':2021,'net_return':.1}),
                    ('EXPERIMENT_CONTRACT.json',20,'c'*64,{'contract_version':1,'result_artifacts':{'COSTED_RESULTS.csv':'a'*64},'metric_units':{'net_return':'fraction'}}),
                    ('COSTED_RESULTS.csv',10,'b'*64,{'year':2021,'net_return':.9})]:
                    ident=uuid.uuid4()
                    conn.execute('INSERT INTO experiment_artifacts VALUES(%s,%s,%s,%s,%s,%s,%s)',
                        (ident,'research/fixture/'+name,'fixture',cutoff-dt.timedelta(seconds=age),name.rsplit('.',1)[1],'sql_records',sha))
                    conn.execute('INSERT INTO artifact_records VALUES(%s,0,%s)',(ident,jsonb(payload)))
                class Fixture:
                    def execute(self,sql,params):
                        return conn.execute(sql.replace('research.experiment_artifacts','pg_temp.experiment_artifacts').replace('research.artifact_records','pg_temp.artifact_records'),params)
                old=load_experiment_results(Fixture(),cutoff-dt.timedelta(seconds=15),'fixture')
                self.assertEqual(old[0]['metrics']['net_return'],.1)
                self.assertEqual(load_experiment_results(Fixture(),cutoff,'fixture'),[])
            finally:conn.rollback()

    @unittest.skipUnless(os.getenv('FINAGENT_TEST_DB')=='1','requires read-only local database')
    def test_sql_catalog_and_experiment_pages_preserve_contract(self):
        from scripts.analytics.common import connect
        cutoff=dt.datetime.now(dt.timezone.utc)
        with connect(autocommit=False) as conn:
            conn.execute('SET TRANSACTION READ ONLY');conn.execute("SET LOCAL statement_timeout='8s'")
            catalog=load_data_catalog(conn,cutoff,['research.experiments'])
            self.assertEqual(len(catalog),1);self.assertIsNone(catalog[0]['canonical_relation'])
            filters={'horizon':20,'cost':15,'cohort':'cross_section_reserve'}
            first=load_experiment_results(conn,cutoff,'eps-expanded-history-20261015',20,filters)
            second=load_experiment_results(conn,cutoff,'eps-expanded-history-20261015',20,filters,20)
            self.assertEqual(len(first)+len(second),35)
            self.assertFalse({r['row_number'] for r in first}&{r['row_number'] for r in second})
            for row in first+second:
                self.assertTrue(row['metric_units']);self.assertEqual(row['gate_status'],'failed')
                if str(row['metrics']['year'])=='2021':self.assertEqual(row['training_status'],'fixed_rule_diagnostic')

    @unittest.skipUnless(os.getenv('FINAGENT_TEST_DB')=='1','requires temporary SQL tables')
    def test_capital_and_holdings_filter_separate_comparable_accounts(self):
        from scripts.analytics.common import connect,jsonb
        import uuid
        cutoff=dt.datetime.now(dt.timezone.utc)
        with connect(autocommit=False) as conn:
            try:
                conn.execute("SET LOCAL statement_timeout='5s'")
                conn.execute('''CREATE TEMP TABLE experiment_artifacts(
                  artifact_id uuid,relative_path text,project_key text,registered_at timestamptz,
                  artifact_kind text,payload_status text,content_sha256 text) ON COMMIT DROP''')
                conn.execute('CREATE TEMP TABLE artifact_records(artifact_id uuid,row_number int,payload jsonb) ON COMMIT DROP')
                ident=uuid.uuid4()
                conn.execute('INSERT INTO experiment_artifacts VALUES(%s,%s,%s,%s,%s,%s,%s)',
                    (ident,'research/fixture/RESULTS.csv','fixture',cutoff,'csv','sql_records','a'*64))
                for row_number,(capital,holdings) in enumerate([(1000000,100),(10000000,100),(1000000,40)]):
                    conn.execute('INSERT INTO artifact_records VALUES(%s,%s,%s)',(ident,row_number,
                        jsonb({'initial_cash':str(capital)+'.0','holdings':str(holdings),'net_return':.1})))
                class Fixture:
                    def execute(self,sql,params):
                        return conn.execute(sql.replace('research.experiment_artifacts','pg_temp.experiment_artifacts').replace('research.artifact_records','pg_temp.artifact_records'),params)
                selected=load_experiment_results(Fixture(),cutoff,'fixture',20,{'initial_cash':1000000,'holdings':100.0})
                self.assertEqual([row['row_number'] for row in selected],[0])
                self.assertEqual(load_experiment_results(Fixture(),cutoff,'fixture',20,{'initial_cash':10000}),[])
                with self.assertRaises(ValueError):load_experiment_results(Fixture(),cutoff,filters={'initial_cash;DROP':'0'})
            finally:conn.rollback()


if __name__=='__main__':unittest.main()
