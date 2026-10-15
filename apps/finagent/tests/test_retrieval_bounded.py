from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('retrieval_helper_test', ROOT / 'MMClaw/mmclaw/finance_db_helper.py')
helper = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(helper)


class EmbeddingConfigurationTests(unittest.TestCase):
    def test_direct_tool_uses_configured_model_and_environment_override(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, '.env').write_text('\n'.join([
                'FINANCE_EMBEDDING_PROVIDER=openai_compatible',
                'FINANCE_EMBEDDING_MODEL=configured-current-model',
                'FINANCE_EMBEDDING_DIMENSIONS=1024',
                'FINANCE_EMBEDDING_BASE_URL=http://127.0.0.1:8082/v1',
                'FINANCE_EMBEDDING_API_KEY_ENV=TEST_EMBEDDING_KEY',
                'TEST_EMBEDDING_KEY=test-only-placeholder',
            ]))
            for overrides, expected, dimensions in (
                ({}, 'configured-current-model', 1024),
                ({'FINANCE_EMBEDDING_MODEL': 'explicit-model',
                  'FINANCE_EMBEDDING_DIMENSIONS': '512'}, 'explicit-model', 512),
            ):
                with self.subTest(expected=expected):
                    response = Mock()
                    response.json.return_value = {'data': [{'embedding': [1.] + [0.] * (dimensions - 1)}]}
                    conn = Mock()
                    conn.execute.return_value.fetchall.return_value = []
                    conn.execute.return_value.fetchone.return_value = {}
                    context = Mock()
                    context.__enter__ = Mock(return_value=conn)
                    context.__exit__ = Mock(return_value=False)
                    with patch.dict(os.environ, {'XTRASTOCK_ENV_FILE': str(Path(root, '.env')), **overrides}, clear=True), \
                         patch.object(helper.requests, 'post', return_value=response) as post, \
                         patch.object(helper, 'connect', return_value=context):
                        _, meta = helper.semantic_document_rows(root, 'test query', [], 30, '', 5, 300)
                    self.assertTrue(meta['semantic_used'])
                    self.assertEqual(meta['embedding_model'], expected)
                    self.assertEqual(meta['embedding_dimensions'], dimensions)
                    self.assertEqual(post.call_args.kwargs['json']['model'], expected)
                    self.assertEqual(post.call_args.args[0], 'http://127.0.0.1:8082/v1/embeddings')
                    self.assertNotIn('test-only-placeholder', json.dumps(meta))


@unittest.skipUnless(os.getenv('FINAGENT_TEST_DB') == '1', 'opt-in isolated PostgreSQL fixture')
class FullTextCandidateContractTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from scripts.pipeline.common import pg_kwargs
        self.conn = psycopg.connect(**pg_kwargs(admin=True), connect_timeout=3)
        self.conn.execute("SET LOCAL statement_timeout='8s'")
        self.conn.execute('''CREATE TEMP TABLE retrieval_fixture (
            document_id uuid PRIMARY KEY, source_type text, source_name text,
            published_at timestamptz, title text, url text, keywords text[],
            stock_codes text[], industries text[], event_types text[],
            search_text text, search_vector tsvector, metadata jsonb
        ) ON COMMIT DROP''')
        self.cutoff = dt.datetime(2026, 10, 13, 12, tzinfo=dt.timezone.utc)
        self.ids = {}
        fixture = [
            ('tail', 'ordinary title', '无关内容。' * 20000 + '光模块量产订单 金额-1,234,567.89元', -1, '000001', 'research_report'),
            ('title', '光模块量产订单标题', '光模块量产订单标题 短报告', -2, '000001', 'research_report'),
            ('positive', 'number', '金额1,234,567.89元', -1, '000001', 'research_report'),
            ('substring', 'number', '金额-91,234,567.89元', -1, '000001', 'research_report'),
            ('future', '光模块', '光模块量产订单', 1, '000001', 'research_report'),
            ('old', '光模块', '光模块量产订单', -90, '000001', 'research_report'),
            ('other_stock', '光模块', '光模块量产订单', -1, '000002', 'research_report'),
            ('other_source', '光模块', '光模块量产订单', -1, '000001', 'news'),
        ]
        for name, title, body, offset, stock, source in fixture:
            ident = uuid.uuid4(); self.ids[name] = str(ident)
            self.conn.execute('''INSERT INTO retrieval_fixture VALUES
                (%s,%s,'fixture',%s,%s,'https://example.invalid',ARRAY[]::text[],%s,
                 ARRAY[]::text[],ARRAY[]::text[],%s,to_tsvector('simple',%s),%s::jsonb)''',
                (ident, source, self.cutoff + dt.timedelta(days=offset), title, [stock],
                 body, title, json.dumps({'fts_vector_prefix_only': name == 'tail'})))
        conn = self.conn
        class FixtureConnection:
            def transaction(self):return conn.transaction()
            def execute(self, sql, params=None):
                return conn.execute(sql.replace('corpus.document_search_index', 'pg_temp.retrieval_fixture'), params)
            def rollback(self):
                # Propagate query failures in a failed transaction to the test;
                # never hide a fixture failure by rebuilding the test table.
                pass
        self.fixture_conn = FixtureConnection()

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def test_full_body_tail_and_title_obey_time_stock_and_source_filters(self):
        rows, meta = helper.fts_document_rows(
            self.fixture_conn, '光模块量产订单', ['光模块', '量产', '订单'],
            ['research_report'], 30, '000001', 10, 300, True, self.cutoff)
        self.assertTrue(meta['fts_used'], meta)
        self.assertEqual({r['id'] for r in rows}, {self.ids['tail'], self.ids['title']})
        tail = next(r for r in rows if r['id'] == self.ids['tail'])
        self.assertIn('光模块量产订单', tail['snippet'])
        self.assertFalse(meta['fts_recall_exhaustive'])

    def test_index_preference_is_restored_on_success_and_query_error(self):
        before=self.conn.execute("SHOW enable_seqscan").fetchone()['enable_seqscan']
        helper.fts_document_rows(self.fixture_conn,'光模块量产订单',['光模块'],[],30,'',5,300,True,self.cutoff)
        self.assertEqual(self.conn.execute("SHOW enable_seqscan").fetchone()['enable_seqscan'],before)
        original=self.fixture_conn.execute
        def broken(sql,params=None):
            if 'WITH bounds AS' in sql:return self.conn.execute('SELECT 1/0')
            return original(sql,params)
        with patch.object(self.fixture_conn,'execute',side_effect=broken):
            rows,meta=helper.fts_document_rows(self.fixture_conn,'光模块',['光模块'],[],30,'',5,300,True,self.cutoff)
        self.assertEqual(rows,[]);self.assertFalse(meta['fts_used'])
        self.assertEqual(self.conn.execute("SHOW enable_seqscan").fetchone()['enable_seqscan'],before)
        self.assertEqual(self.conn.execute('SELECT 1 AS n').fetchone()['n'],1)

    def test_exact_signed_number_in_full_body_tail_rejects_substrings(self):
        rows, meta = helper.fts_document_rows(
            self.fixture_conn, '-1234567.89', ['-1234567.89'],
            ['research_report'], 30, '000001', 10, 300, True, self.cutoff)
        self.assertTrue(meta['fts_used'], meta)
        self.assertEqual({r['id'] for r in rows}, {self.ids['tail']})
        self.assertIn('-1,234,567.89', rows[0]['snippet'])


if __name__ == '__main__':
    unittest.main()
