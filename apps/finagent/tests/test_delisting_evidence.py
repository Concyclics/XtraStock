import copy
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
import io
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/analytics'))
from import_delisting_evidence import validate_manifest, apply_rows, SOURCE
from lifecycle_evidence import lifecycle_order
from sync_asset_lifecycle_v3 import BASELINE_SQL
from common import connect


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'source.json'
        self.raw = {'result': [{'A_STOCK_CODE': '600077', 'COMPANY_ABBR': 'sample', 'LIST_DATE': '19970520',
                               'DELIST_DATE': '20230725', 'STATE_CODE': '3'}]}
        self.path.write_text(json.dumps(self.raw))
        self.item = {'exchange': 'SSE', 'symbol': '600077', 'delisted_date': '2023-07-25', 'reviewed': True,
                     'source': {'format': 'sse_delisted_json', 'file': str(self.path),
                                'url': 'https://query.sse.com.cn/commonQuery.do?sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L&COMPANY_STATUS=3',
                                'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
                                'observed_at': '2026-10-15T00:00:00+00:00'}}

    def tearDown(self):
        self.tmp.cleanup()

    def parse(self):
        return validate_manifest({'schema': 'reviewed-delisting-v1', 'items': [self.item]})

    def test_delisting_is_distinct_from_last_trading_day(self):
        self.assertEqual(self.parse()[0]['delisted_date'], '2023-07-25')
        self.item['delisted_date'] = '2023-06-13'
        with self.assertRaisesRegex(ValueError, 'not supported'):
            self.parse()

    def test_identity_hash_endpoint_and_time_are_checked(self):
        for key, value in [('sha256', '0' * 64), ('url', 'https://sse.com.cn.example/list'),
                           ('observed_at', '2026-10-15T00:00:00'), ('observed_at', '2099-01-01T00:00:00+00:00')]:
            with self.subTest(key=key, value=value):
                old = self.item['source'][key]
                self.item['source'][key] = value
                with self.assertRaises(ValueError):
                    self.parse()
                self.item['source'][key] = old
        self.item['symbol'] = '600078'
        with self.assertRaisesRegex(ValueError, 'identity absent'):
            self.parse()

    def test_duplicate_requested_identity_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate_manifest({'schema': 'reviewed-delisting-v1', 'items': [self.item, copy.deepcopy(self.item)]})

    @unittest.skipUnless(importlib.util.find_spec('openpyxl'), 'analytics XLSX dependency')
    def test_szse_xlsx_uses_termination_not_listing_date(self):
        from openpyxl import Workbook
        path = Path(self.tmp.name) / 'source.xlsx'
        workbook = Workbook(); workbook.active.append(['证券代码', '证券简称', '上市日期', '终止上市日期'])
        workbook.active.append(['002781', 'sample', '2015-12-22', '2023-07-05']); workbook.save(path)
        self.item.update(exchange='SZSE', symbol='002781', delisted_date='2023-07-05')
        self.item['source'].update(format='szse_delisted_xlsx', file=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                   url='https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=1793_ssgs&TABKEY=tab2')
        self.assertEqual(self.parse()[0]['delisted_date'], '2023-07-05')
        # Reproduce the exchange file's stale worksheet dimension metadata.
        buffer = io.BytesIO()
        with zipfile.ZipFile(path) as old, zipfile.ZipFile(buffer, 'w') as new:
            for name in old.namelist():
                raw = old.read(name)
                if name == 'xl/worksheets/sheet1.xml':
                    raw = re.sub(rb'<dimension ref="[^"]+"', b'<dimension ref="A1:A1"', raw)
                new.writestr(name, raw)
        path.write_bytes(buffer.getvalue())
        self.item['source']['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.parse()[0]['delisted_date'], '2023-07-05')

    def test_order_alias_cannot_inject_sql(self):
        with self.assertRaises(ValueError):
            lifecycle_order('v;DELETE')


class TempConnection:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        for table in ('asset_lifecycle_versions', 'assets', 'bars_daily', 'canonical_bars_daily_v2'):
            sql = sql.replace('market.' + table, 'test_' + table)
        return self.conn.execute(sql, params)


@unittest.skipUnless(os.environ.get('FINAGENT_DB_TESTS') == '1', 'explicit rollback-only PostgreSQL fixtures')
class LifecycleSQLTests(unittest.TestCase):
    def setUp(self):
        self.raw_conn = connect(autocommit=False)
        self.c = TempConnection(self.raw_conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.asset = uuid.uuid4()
        self.c.execute("""CREATE TEMP TABLE test_assets(asset_id uuid PRIMARY KEY,asset_type text,symbol text,
            exchange text,name text,listed_date date,delisted_date date,lifecycle_status text,metadata jsonb,
            updated_at timestamptz DEFAULT now());
            CREATE TEMP TABLE test_bars_daily(asset_id uuid,ts timestamptz,close float);
            CREATE TEMP TABLE test_canonical_bars_daily_v2(asset_id uuid,dt date,close float,quality_status text);
            CREATE TEMP TABLE test_asset_lifecycle_versions(lifecycle_version_id bigserial,asset_id uuid,
            valid_from date,valid_to date,lifecycle_status text,is_tradable bool,is_st bool,is_suspended bool,
            exchange text,source_name text,source_document_id uuid,available_at timestamptz,
            quality_status text,metadata jsonb,created_at timestamptz DEFAULT now())""")
        self.c.execute("INSERT INTO market.assets VALUES(%s,'stock','600077','SSE','sample','1997-05-20',NULL,'active','{}',now())", (self.asset,))
        self.c.execute("INSERT INTO market.bars_daily VALUES(%s,'2023-06-13 07:00:00+00',1)", (self.asset,))
        self.c.execute("INSERT INTO market.canonical_bars_daily_v2 VALUES(%s,'2023-06-13',1,'verified')", (self.asset,))
        self.row = dict(symbol='600077', exchange='SSE', delisted_date='2023-07-25', listed_date='1997-05-20',
                        source={'observed_at': '2026-10-15T00:00:00+00:00', 'sha256': 'a' * 64}, reviewed=True)

    def tearDown(self):
        self.raw_conn.rollback(); self.raw_conn.close(); self.tmp.cleanup()

    def baseline(self):
        return self.c.execute(BASELINE_SQL, {'as_of': dt.date(2026, 10, 15), 'asset_ids': [self.asset]}).fetchone()

    def test_import_idempotent_and_does_not_backdate_knowledge(self):
        before = self.c.execute('SELECT now() t').fetchone()['t']
        first = apply_rows(self.c, [self.row], Path(self.tmp.name), apply=True)
        again = apply_rows(self.c, [self.row], Path(self.tmp.name), apply=True)
        self.assertEqual(first['lifecycle_inserted'], 1); self.assertEqual(again['lifecycle_inserted'], 0)
        self.assertEqual(again['master_updated'], 0)
        row = self.c.execute('SELECT * FROM market.asset_lifecycle_versions').fetchone()
        self.assertEqual(row['valid_from'], dt.date(2023, 7, 25)); self.assertGreaterEqual(row['available_at'], before)
        self.assertEqual(row['quality_status'], 'verified'); self.assertFalse(row['is_tradable'])
        self.assertFalse(row['metadata']['is_st_verified'])

    def test_price_after_delisting_aborts_before_any_writes(self):
        self.c.execute("INSERT INTO market.bars_daily VALUES(%s,'2023-07-25 07:00:00+00',1)", (self.asset,))
        with self.assertRaisesRegex(ValueError, 'price on/after'):
            apply_rows(self.c, [self.row], Path(self.tmp.name), apply=True)
        self.assertEqual(self.c.execute('SELECT count(*) n FROM market.asset_lifecycle_versions').fetchone()['n'], 0)

    def test_missing_old_prices_do_not_prove_delisting(self):
        self.c.execute("INSERT INTO market.bars_daily VALUES(%s,'2026-10-16 07:00:00+00',1)", (self.asset,))
        self.baseline()
        rows = self.c.execute('SELECT * FROM market.asset_lifecycle_versions ORDER BY valid_from').fetchall()
        self.assertEqual([r['lifecycle_status'] for r in rows], ['active', 'unknown'])
        self.assertEqual(rows[-1]['valid_from'], dt.date(2023, 6, 14))
        self.assertEqual(self.baseline()['inserted'], 0)

    def test_delisting_inclusive_preserves_gap_and_prior_versions(self):
        self.baseline()
        old = self.c.execute('SELECT count(*) n FROM market.asset_lifecycle_versions').fetchone()['n']
        apply_rows(self.c, [self.row], Path(self.tmp.name), apply=True)
        self.baseline()
        self.assertGreater(self.c.execute('SELECT count(*) n FROM market.asset_lifecycle_versions').fetchone()['n'], old)
        rows = self.c.execute("SELECT * FROM market.asset_lifecycle_versions WHERE source_name='market.assets baseline:v3' AND lifecycle_status='delisted'").fetchall()
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['valid_from'], dt.date(2023, 7, 25))
        gap = self.c.execute("SELECT * FROM market.asset_lifecycle_versions WHERE lifecycle_status='unknown' AND valid_to='2023-07-24'").fetchone()
        self.assertEqual(gap['valid_from'], dt.date(2023, 6, 14))
        selected = self.c.execute(f"SELECT * FROM market.asset_lifecycle_versions WHERE valid_from<='2026-10-15' AND (valid_to IS NULL OR valid_to>='2026-10-15') ORDER BY {lifecycle_order()} LIMIT 1").fetchone()
        self.assertEqual(selected['source_name'], SOURCE)

    def test_only_old_prices_do_not_extend_recent_inference(self):
        self.c.execute("UPDATE market.bars_daily SET ts='2019-01-02 07:00:00+00'")
        self.c.execute("UPDATE market.canonical_bars_daily_v2 SET dt='2019-01-02'")
        self.baseline()
        rows = self.c.execute('SELECT * FROM market.asset_lifecycle_versions').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['lifecycle_status'], 'unknown')
        self.assertEqual(rows[0]['valid_from'], dt.date(2026, 10, 15))
        self.assertEqual(rows[0]['metadata']['observed_price_window_start'], '2021-10-15')

    def test_recent_raw_price_does_not_wait_for_canonical_refresh(self):
        self.c.execute("INSERT INTO market.bars_daily VALUES(%s,'2026-10-15 07:00:00+00',1)", (self.asset,))
        self.baseline()
        row = self.c.execute("SELECT * FROM market.asset_lifecycle_versions WHERE lifecycle_status='active'").fetchone()
        self.assertEqual(row['valid_to'], dt.date(2026, 10, 15))
        self.assertEqual(row['quality_status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
