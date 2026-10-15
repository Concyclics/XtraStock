import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
from scripts.ops.deploy_finagent import deployment_plan, ENABLE, REQUIRED_RELATIONS
from scripts.agent.run_quality_gate import isolated_commands


class RepositoryOpsTests(unittest.TestCase):
    def test_deploy_includes_new_database_consumers(self):
        with tempfile.TemporaryDirectory() as temp:
            rows=deployment_plan(['finance-research-registry.service','finance-cninfo-bodies.timer'],Path(temp))
            self.assertTrue(all(r['changed'] for r in rows))
            for row in rows:
                (Path(temp)/row['unit']).write_text(row['rendered_content'])
            self.assertFalse(any(r['changed'] for r in deployment_plan([r['unit'] for r in rows],Path(temp))))
        self.assertIn('ops.data_asset_registry',REQUIRED_RELATIONS)
        self.assertNotIn('mmclaw-wechat-agent.service',ENABLE)
        with self.assertRaises(ValueError):deployment_plan(['../../unknown'],Path('/tmp'))

    def test_deploy_preserves_batch_embedding_and_configured_research_dropins(self):
        rows=deployment_plan(['finance-document-embedding.service','finance-factor-research.service'],Path('/tmp/finagent-test-units'))
        paths={r['relative_path'] for r in rows}
        self.assertIn('finance-document-embedding.service.d/40-batch-processing.conf',paths)
        self.assertIn('finance-factor-research.service.d/research-pool.conf',paths)

    def test_common_modules_are_isolated_and_live_probe_is_preserved(self):
        probe=['python','scripts/agent/smoke_finance_v3_tools.py']
        result=isolated_commands([['python','-m','unittest','tests.one','tests.two','-v'],probe])
        self.assertEqual(len(result),3)
        self.assertEqual(result[0],['python','-m','unittest','tests.one','-v'])
        self.assertEqual(result[-1],probe)

    def test_runtime_private_state_location_survives_code_move(self):
        spec=importlib.util.spec_from_file_location('runtime_paths_test',ROOT/'runtime/runtime_paths.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertEqual(module.RUNTIME_STATE,ROOT.parents[1]/'.local/state/gateway')
        self.assertEqual((ROOT/'runtime/model_lifecycle.py').resolve(),ROOT.parents[1]/'services/model_gateway/model_lifecycle.py')


if __name__=='__main__':unittest.main()
