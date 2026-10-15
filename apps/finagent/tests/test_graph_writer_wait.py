"""Exercise contention and rollback with an isolated PostgreSQL advisory key."""
import os
import sys
import threading
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from common import connect
from graph_revision_policy import acquire_graph_write_lock
from psycopg.errors import LockNotAvailable

@unittest.skipUnless(os.getenv('FINAGENT_DB_TESTS') == '1', 'requires local DB')
class GraphWriterWaitTests(unittest.TestCase):
    def test_wait_release_restore_and_timeout(self):
        key = 1800000000 + os.getpid()
        with connect(autocommit=False) as holder, connect(autocommit=False) as waiter:
            holder.execute('SELECT pg_advisory_xact_lock(%s)', (key,))
            released = threading.Event()
            def release():
                time.sleep(.2)
                holder.rollback()
                released.set()
            thread = threading.Thread(target=release)
            thread.start()
            with waiter.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout='5s'")
                acquire_graph_write_lock(cur, timeout_ms=2000, lock_key=key)
                self.assertTrue(released.wait(1))
                self.assertEqual(cur.execute("SHOW lock_timeout").fetchone()['lock_timeout'], '5s')
            thread.join()
            self.assertFalse(holder.execute('SELECT pg_try_advisory_xact_lock(%s) ok', (key,)).fetchone()['ok'])
            holder.rollback()
            waiter.rollback()
            holder.execute('SELECT pg_advisory_xact_lock(%s)', (key,))
            with self.assertRaises(LockNotAvailable), waiter.cursor() as cur:
                acquire_graph_write_lock(cur, timeout_ms=100, lock_key=key)
            waiter.rollback()
            holder.rollback()
            self.assertTrue(waiter.execute('SELECT pg_try_advisory_xact_lock(%s) ok', (key,)).fetchone()['ok'])
            waiter.rollback()

if __name__ == '__main__':
    unittest.main()
