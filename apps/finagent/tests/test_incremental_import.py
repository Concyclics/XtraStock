import importlib.util
from pathlib import Path
import tempfile
import unittest
import sys

pipeline = Path(__file__).resolve().parents[1] / "scripts/pipeline"
sys.path.insert(0, str(pipeline))
spec = importlib.util.spec_from_file_location("incremental_import_tested", pipeline / "incremental_import.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class IncrementalImportTests(unittest.TestCase):
    def test_source_row_and_file_changes_invalidate_version(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "article.txt"
            p.write_text("original")
            a = module.fingerprint({"title": "one"}, [p])
            self.assertEqual(a, module.fingerprint({"title": "one"}, [p]))
            self.assertNotEqual(a, module.fingerprint({"title": "two"}, [p]))
            p.write_text("modified content")
            self.assertNotEqual(a, module.fingerprint({"title": "one"}, [p]))
            p.unlink()
            self.assertNotEqual(a, module.fingerprint({"title": "one"}, [p]))

    def test_staging_is_session_local_and_transaction_owned(self):
        for source in module.SOURCES:
            create, body = module.sql_for(source)
            self.assertIn("CREATE TEMP TABLE", create)
            self.assertIn("ON COMMIT DROP", create)
            self.assertNotIn("staging." + module.SOURCES[source][0], body)
            self.assertNotIn("TRUNCATE", body)
            self.assertNotIn("COMMIT;", body)
            self.assertNotIn("ANALYZE ", body)
            self.assertNotIn("AS staging_rows", body)
            if source in ("peoples","xinhua"):
                self.assertNotIn("upserted_documents AS",body)

    def test_materialization_failure_does_not_advance_ledger(self):
        from unittest.mock import MagicMock
        conn = MagicMock()
        def execute(sql, *args):
            if "INSERT INTO corpus.documents" in sql:
                raise RuntimeError("injected materialization failure")
        conn.execute.side_effect = execute
        item = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "injected"):
            module.write_batch(conn, "eastmoney", [("test", "version", item)])
        conn.cursor.return_value.__enter__.return_value.executemany.assert_not_called()
        self.assertIsNotNone(conn.transaction.return_value.__exit__.call_args.args[0])

    def test_only_transient_transaction_errors_get_bounded_retries(self):
        from unittest.mock import patch,MagicMock
        from psycopg.errors import LockNotAvailable
        with patch.object(module,'write_batch_once',side_effect=[LockNotAvailable('busy'),None]) as write,patch.object(module.time,'sleep'):
            module.write_batch(MagicMock(),'peoples',[])
            self.assertEqual(write.call_count,2)
        with patch.object(module,'write_batch_once',side_effect=LockNotAvailable('busy')) as write,patch.object(module.time,'sleep'):
            with self.assertRaises(LockNotAvailable):module.write_batch(MagicMock(),'peoples',[])
            self.assertEqual(write.call_count,3)
