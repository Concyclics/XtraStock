import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(APP), str(APP / 'scripts/analytics')]
from quant_method_library import validate_cards, load_methods, register_methods
from scripts.analytics.research_intelligence import load_intelligence, intelligence_rows
from scripts.analytics.research_asset_registry import project_roots


class FakeConnection:
    def __init__(self, card):
        self.card = card
        self.calls = []

    def execute(self, query, args):
        self.calls.append((query, args))
        return self

    def fetchall(self):
        return [{'method_key': self.card['method_key'], 'payload': self.card,
                 'registered_at': dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)}]

    def fetchone(self):
        return {'method_version_id': 'test'}


class MethodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = json.loads((APP.parents[1] / 'config/research/methods.json').read_text())

    def test_primary_provenance_required(self):
        self.assertGreaterEqual(len(validate_cards(self.library)), 6)
        broken = copy.deepcopy(self.library)
        del broken['methods'][0]['source']['scope']
        with self.assertRaises(ValueError):
            validate_cards(broken)

    def test_duplicate_keys_rejected(self):
        broken = copy.deepcopy(self.library)
        broken['methods'].append(broken['methods'][0])
        with self.assertRaises(ValueError):
            validate_cards(broken)

    def test_credential_urls_rejected(self):
        broken = copy.deepcopy(self.library)
        broken['methods'][0]['source']['url'] = 'https://' + 'alice:credential' + '@example.org/paper'
        with self.assertRaises(ValueError):
            validate_cards(broken)

    def test_cutoff_and_literal_search_parameterized(self):
        conn = FakeConnection(self.library['methods'][0])
        cutoff = dt.datetime.now(dt.timezone.utc)
        rows = load_methods(conn, cutoff, 1000, "x_%';drop table x;--")
        query, args = conn.calls[-1]
        self.assertIn('registered_at<=%s', query)
        self.assertNotIn('drop table', query)
        self.assertEqual(args[-1], 20)
        self.assertIn(r'\_\%', args[2])
        self.assertFalse(rows[0]['prediction_eligible'])
        self.assertNotIn('content_sha256', rows[0])

    def test_methods_scope_does_not_trigger_stock_or_forecast_queries(self):
        conn = FakeConnection(self.library['methods'][0])
        state = load_intelligence(conn, dt.datetime.now(dt.timezone.utc), scope='methods')
        self.assertEqual(len(conn.calls), 1)
        self.assertEqual(intelligence_rows(state)[0]['component'], 'quant_research_method')

    def test_registry_is_append_only(self):
        conn = FakeConnection(self.library['methods'][0])
        result = register_methods(conn, self.library)
        self.assertEqual(result['inserted'], len(self.library['methods']))
        self.assertTrue(all('DO NOTHING' in q for q, _ in conn.calls))
        self.assertTrue(all('DO UPDATE' not in q for q, _ in conn.calls))

    def test_project_registration_cannot_escape_research_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            (base/'one').mkdir()
            (base/'two').mkdir()
            self.assertEqual(project_roots(base,['one']),[base/'one'])
            with self.assertRaises(ValueError):
                project_roots(base,['../two'])
            (base/'link').symlink_to(base/'two',target_is_directory=True)
            with self.assertRaises(ValueError):
                project_roots(base,['link'])


if __name__ == '__main__':
    unittest.main()
