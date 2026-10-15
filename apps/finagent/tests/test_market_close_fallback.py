import contextlib
import datetime as dt
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/pipeline"))
spec = importlib.util.spec_from_file_location("close_job", ROOT / "scripts/pipeline/daily_market_close.py")
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


class CloseFallbackTests(unittest.TestCase):
    def run_job(self, probe_fails=False, fallback_fails=False):
        calls = []

        def run(command, log_file):
            calls.append(command)
            if probe_fails and "Spider/eastmoney/scripts/crawl_daily_pg.py" in command:
                raise subprocess.CalledProcessError(1, command)
            if fallback_fails and "scripts/pipeline/market_kline_sina_backfill.py" in command:
                raise subprocess.CalledProcessError(1, command)

        with mock.patch.object(sys, "argv", ["daily_market_close.py", "--force"]), mock.patch.object(job, "pipeline_lock", return_value=contextlib.nullcontext()), mock.patch.object(job, "crawler_run", return_value=contextlib.nullcontext()), mock.patch.object(job, "log_path", return_value=Path("unused.log")), mock.patch.object(job, "today", return_value=dt.date(2026, 10, 8)), mock.patch.object(job, "run_logged", side_effect=run):
            result = job.main()
        return result, calls

    def test_unavailable_primary_reaches_sina_without_full_market_retry(self):
        result, calls = self.run_job(probe_fails=True)
        self.assertEqual(0, result)
        primary = next(c for c in calls if "Spider/eastmoney/scripts/crawl_daily_pg.py" in c)
        self.assertIn("--max-seconds", primary)
        self.assertIn("--health-output", primary)
        fallback = next(c for c in calls if "scripts/pipeline/market_kline_sina_backfill.py" in c)
        self.assertIn("stock", fallback)
        self.assertIn("index", fallback)
        self.assertTrue(any("scripts/analytics/sync_canonical_daily_bars.py" in c for c in calls))

    def test_available_primary_keeps_original_crawl(self):
        result, calls = self.run_job()
        self.assertEqual(0, result)
        self.assertTrue(any("Spider/eastmoney/scripts/crawl_daily_pg.py" in c for c in calls))

    def test_failed_fallback_cannot_be_reported_success(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_job(probe_fails=True, fallback_fails=True)


if __name__ == "__main__":
    unittest.main()
