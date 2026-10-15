from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/analytics'))
from collect_research_history import make_plan, parse_response


class HistoricalCollectionTests(unittest.TestCase):
    def setUp(self):
        self.job = {'symbol': '600005', 'provider_symbol': 'sh600005', 'start': '2010-01-01', 'end': '2011-12-31'}

    def response(self, rows):
        return {'code': 0, 'data': {'sh600005': {'day': rows}}}

    def test_normalizes_provider_open_close_high_low_order(self):
        rows = parse_response(self.response([['2010-01-04', '8.35', '8.18', '8.38', '8.17', '565176.68']]), self.job)
        self.assertEqual(rows[0]['close'], 8.18)
        self.assertEqual(rows[0]['high'], 8.38)
        self.assertEqual(rows[0]['volume'], 565176.68)

    def test_bad_dates_prices_duplicates_and_error_payload_are_rejected(self):
        good = ['2010-01-04', '8.35', '8.18', '8.38', '8.17', '50']
        for payload in [self.response([['2020-01-04', *good[1:]]]), self.response([good, good]),
                        self.response([['2010-01-04', '-8', '-9', '-7', '-10', '50']]),
                        {'code': 1, 'data': 'invalid param'}]:
            with self.assertRaises(ValueError):
                parse_response(payload, self.job)

    def test_empty_is_explicit_and_not_fabricated(self):
        self.assertEqual(parse_response(self.response([]), self.job), [])

    def test_delisted_unknown_ipo_is_kept_beside_deterministic_sample(self):
        master = [{'symbol': '600001', 'exchange': 'SSE', 'listed_date': None, 'lifecycle_status': 'delisted'},
                  {'symbol': '600002', 'exchange': 'SSE', 'listed_date': '2005-01-01', 'lifecycle_status': 'active'},
                  {'symbol': '600003', 'exchange': 'SSE', 'listed_date': '2006-01-01', 'lifecycle_status': 'active'}]
        plan = make_plan(master, 1)
        self.assertEqual(plan, make_plan(list(reversed(master)), 1))
        self.assertIn('600001', [a['symbol'] for a in plan['securities']])
        self.assertFalse(plan['historical_universe_complete'])
        self.assertEqual(plan['delisting_evidence_included'], 1)

    def test_default_collection_stays_inside_recent_five_years(self):
        master = [{'symbol': '600001', 'exchange': 'SSE', 'listed_date': '1993-01-01', 'lifecycle_status': 'active'}]
        plan = make_plan(master, 1, as_of='2026-10-16')
        self.assertEqual(plan['primary_since'], '2021-10-16')
        self.assertTrue(all('2021-10-16' <= j['start'] <= j['end'] <= '2026-10-16' for j in plan['jobs']))
        self.assertEqual(plan['jobs'][-1]['end'], '2026-10-16')


if __name__ == '__main__':
    unittest.main()
