import datetime as dt
import unittest
from scripts.pipeline.repair_windows import choose_window


class WindowTests(unittest.TestCase):
    def test_explicit_date_does_not_expand_daily_work(self):
        day=dt.date(2026,10,13)
        self.assertEqual(choose_window(dt.date(2020,1,1),day,'daily'),dt.date(2026,10,10))

    def test_repair_preserves_old_dirty_date(self):
        old=dt.date(2020,1,1)
        self.assertEqual(choose_window(old,dt.date(2026,10,13),'repair'),old)

    def test_repair_flag_required(self):
        with self.assertRaises(ValueError):
            choose_window(dt.date(2026,10,1),dt.date(2026,10,13),'daily',repair_since=dt.date(2020,1,1))
