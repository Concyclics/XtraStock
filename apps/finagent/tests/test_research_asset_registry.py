import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from scripts.analytics.research_asset_registry import read_records,walk_artifacts,load_experiment_results,safe_payload

class RegistryTests(unittest.TestCase):
 def test_small_results_import_but_sensitive_keys_do_not(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'RESULTS.json';p.write_text(json.dumps({'net_return':.12,'horizon_days':20}))
   self.assertEqual(read_records(p),([{'net_return':.12,'horizon_days':20}],'sql_records'))
   p.write_text(json.dumps({'api_key':'redacted-test','net_return':.12}))
   rows,status=read_records(p);self.assertEqual(rows,[]);self.assertEqual(status,'registered_file_payload_not_imported')
 def test_code_backup_and_symlink_are_not_ingested(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'results.csv').write_text('a,b\n1,2\n')
   (root/'code-before').mkdir();(root/'code-before'/'env.json').write_text('{}')
   (root/'link.json').symlink_to(root/'code-before'/'env.json')
   self.assertEqual([p.name for p in walk_artifacts(root)],['results.csv'])
 def test_cutoff_and_project_bound_and_no_promotion_or_paths(self):
  conn=Mock();conn.execute.return_value.fetchall.return_value=[{'project_key':'trial','artifact_id':'ref',
    'available_at':'2026-10-15T10:00:00+08:00','row_number':0,
    'payload':{'net_return':'0.1','year':'2024','path':'/private/file','content_hash':'internal','api_key':'test'}}]
  at=dt.datetime.now(dt.timezone.utc);rows=load_experiment_results(conn,at,'trial',100)
  self.assertEqual(conn.execute.call_args.args[1][:3],(at,'trial','trial'))
  self.assertEqual(conn.execute.call_args.args[1][-1],20)
  self.assertEqual(rows[0]['metrics'],{'net_return':'0.1','year':'2024'})
  self.assertFalse(rows[0]['prediction_eligible'])
  self.assertEqual(rows[0]['usage_status'],'research_only')
 def test_bearer_value_rejected_even_under_generic_key(self):
  with self.assertRaises(ValueError):safe_payload({'value':'Bearer private-test-value'})
if __name__=='__main__':unittest.main()
