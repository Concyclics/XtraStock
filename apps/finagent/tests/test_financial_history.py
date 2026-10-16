import copy
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from scripts.analytics.financial_history import (
    FEATURE_NAMES, MODES, build_feature_panel, distinct_versions, normalize_event,
)

ROOT = Path(__file__).resolve().parents[1]
ASSET = {'asset_id': 'asset-a', 'symbol': '600909', 'exchange': 'SSE'}


def event(event_id=1, period='2021-12-31', notice='2022-03-31', updated='2023-03-30', **fields):
    return {'id': event_id, 'asset_id': ASSET['asset_id'], 'event_date': period,
        'source': 'eastmoney:datacenter', 'created_at': '2026-06-30T02:00:00+00:00',
        'raw_payload': {'SECURITY_CODE': '600909', 'SECUCODE': '600909.SH', 'REPORTDATE': period,
            'NOTICE_DATE': notice, 'UPDATE_DATE': updated, 'XSMLL': 35, 'WEIGHTAVG_ROE': 8,
            'TOTAL_OPERATE_INCOME': 1000, 'PARENT_NETPROFIT': 80, 'YSTZ': 15, 'SJLTZ': -20,
            'BPS': 4, 'MGJYXJJE': .6, **fields}}


def panel(events, dates, mode=MODES[0], max_age_days=550):
    return build_feature_panel([normalize_event(e, ASSET) for e in events], [ASSET], dates,
                               mode=mode, max_age_days=max_age_days)


class FinancialHistoryTests(unittest.TestCase):
    def test_revision_date_delays_current_value_instead_of_backdating_to_notice(self):
        row = normalize_event(event(), ASSET)
        self.assertEqual(row['publication_delay_days'], 364)
        values, ids, audit = panel([event()], ['2022-04-01', '2023-03-30', '2023-03-31'])
        self.assertEqual(ids[:, 0].tolist(), [-1, -1, 1])
        np.testing.assert_allclose(values[2, 0, :5], [.35, .08, .08, .15, -.2])
        self.assertEqual(values[2, 0, 5], 12)
        self.assertEqual(audit['selected_asset_days'], 1)

    def test_actual_ingestion_after_close_cannot_enter_same_day_system_view(self):
        row = event(period='2026-03-31', notice='2026-04-28', updated='2026-04-28')
        row['created_at'] = '2026-06-30T07:01:00+00:00'
        public, _, _ = panel([row], ['2026-06-30', '2026-07-01'])
        observed, ids, _ = panel([row], ['2026-06-30', '2026-07-01'], mode=MODES[1])
        self.assertTrue(np.isfinite(public).all())
        self.assertTrue(np.isnan(observed[0]).all())
        self.assertEqual(ids[:, 0].tolist(), [-1, 1])

    def test_source_ingestion_date_is_conservative_if_later_than_vendor_update(self):
        row = event(EITIME='2023-04-03 18:45:00')
        _, ids, _ = panel([row], ['2023-03-31', '2023-04-03', '2023-04-04'])
        self.assertEqual(ids[:, 0].tolist(), [-1, -1, 1])

    def test_later_revision_of_old_period_does_not_displace_newer_period(self):
        old = event()
        new = event(2, '2022-12-31', '2023-03-01', '2023-03-01', XSMLL=41)
        values, ids, _ = panel([old, new], ['2023-03-02', '2023-03-31'])
        self.assertEqual(ids[:, 0].tolist(), [2, 2])
        np.testing.assert_allclose(values[:, 0, 0], [.41, .41])

    def test_missing_field_and_period_duration_are_not_borrowed_or_annualized(self):
        old = event(updated='2022-03-31')
        new = event(2, '2022-06-30', '2022-08-20', '2022-08-20', XSMLL=None, WEIGHTAVG_ROE=6)
        values, ids, _ = panel([old, new], ['2022-08-22'])
        self.assertEqual(ids[0, 0], 2)
        self.assertTrue(np.isnan(values[0, 0, 0]))
        self.assertAlmostEqual(float(values[0, 0, 2]), .06)
        self.assertEqual(values[0, 0, 5], 6)

    def test_same_public_clock_conflict_is_not_resolved_using_future_ingestion(self):
        first, second = event(), event(2, XSMLL=51)
        second['created_at'] = '2026-07-02T03:00:00+00:00'
        values, ids, audit = panel([first, second], ['2023-03-31'])
        self.assertTrue(np.isnan(values).all())
        self.assertEqual(ids[0, 0], -1)
        self.assertEqual(audit['ambiguous_versions'], 1)
        later = event(3, updated='2023-04-02', XSMLL=51)
        _, ids, _ = panel([first, second, later], ['2023-03-31', '2023-04-03'])
        self.assertEqual(ids[:, 0].tolist(), [-1, 3])

    def test_identical_values_deduplicate_without_changing_feature_values(self):
        first, second = event(8), event(9)
        second['created_at'] = '2026-07-02T03:00:00+00:00'
        values, ids, audit = panel([second, first], ['2023-03-31'])
        self.assertEqual(ids[0, 0], 8)
        self.assertEqual(audit['same_clock_extra_rows'], 1)
        self.assertEqual(audit['ambiguous_versions'], 0)
        self.assertAlmostEqual(float(values[0, 0, 0]), .35)

    def test_conflict_in_latest_period_does_not_fall_back_to_old_report(self):
        old = event(updated='2022-03-31')
        first = event(2, '2022-06-30', '2022-08-20', '2022-08-20', XSMLL=12)
        second = copy.deepcopy(first); second['id'] = 3; second['raw_payload']['XSMLL'] = 15
        _, ids, _ = panel([old, first, second], ['2022-08-22'])
        self.assertEqual(ids[0, 0], -1)

    def test_staleness_uses_period_end_and_future_rows_cannot_change_past(self):
        old = event(updated='2022-03-31')
        future = event(2, '2022-12-31', '2023-04-01', '2023-04-01', XSMLL=-90)
        dates = ['2022-04-01', '2022-05-01']
        before = panel([old], dates, max_age_days=100)
        after = panel([old, future], dates, max_age_days=100)
        np.testing.assert_equal(before[0], after[0])
        self.assertEqual(before[1][:, 0].tolist(), [1, -1])

    def test_identity_period_and_missing_revision_dates_are_rejected(self):
        changes = [({'SECURITY_CODE': '000001'}, None), ({'SECUCODE': '600909.SZ'}, None),
                   ({'REPORTDATE': '2022-03-31'}, None), ({'UPDATE_DATE': None}, None),
                   ({'NOTICE_DATE': '2021-12-30'}, None), ({}, '2026-06-30T07:00:00')]
        for fields, created in changes:
            row = event(**fields)
            if created:
                row['created_at'] = created
            with self.assertRaises(ValueError):
                normalize_event(row, ASSET)

    def test_nonfinite_false_values_and_invalid_denominator_stay_missing(self):
        row = normalize_event(event(XSMLL=float('inf'), WEIGHTAVG_ROE=True,
                                    TOTAL_OPERATE_INCOME=0, YSTZ='NaN', SJLTZ=-20), ASSET)
        self.assertEqual(list(row['values'].values()), [None, None, None, None, -.2])
        self.assertNotIn('cash_per_share_to_book', FEATURE_NAMES)

    def test_unknown_clock_duplicate_dates_and_foreign_assets_are_errors(self):
        row = normalize_event(event(), ASSET)
        with self.assertRaises(ValueError):
            distinct_versions([row], 'announcement_only')
        with self.assertRaises(ValueError):
            build_feature_panel([row], [ASSET], ['2023-04-01']*2, mode=MODES[0])
        with self.assertRaises(ValueError):
            build_feature_panel([row], [], ['2023-04-01'], mode=MODES[0])

    def test_independent_asof_audit_and_source_checks_reject_tampering(self):
        from scripts.analytics.verify_financial_history_panel import check_source_values, reference_panel
        inputs = [event(updated='2022-03-31'), event(2, '2022-06-30', '2022-08-20', '2022-08-20', XSMLL=None)]
        third = copy.deepcopy(inputs[-1]); third['id'] = 3; third['raw_payload']['YSTZ'] = 99
        inputs.append(third)
        accepted = [normalize_event(r, ASSET) for r in inputs]
        dates = ['2022-04-01', '2022-08-22', '2026-06-30']
        for mode in MODES:
            actual, ids, _ = panel(inputs, dates, mode)
            expected, reference_ids = reference_panel(accepted, [ASSET], dates, mode, 550)
            np.testing.assert_equal(actual, expected)
            np.testing.assert_equal(ids, reference_ids)
        for raw, normalized in zip(inputs, accepted):
            check_source_values(raw, normalized, ASSET)
        corrupted = copy.deepcopy(accepted[0]); corrupted['values']['reported_gross_margin'] = 35
        with self.assertRaises(AssertionError):
            check_source_values(inputs[0], corrupted, ASSET)
        corrupted = copy.deepcopy(accepted[0]); corrupted['public_at'] = '2022-03-31T00:00:00+00:00'
        with self.assertRaises(AssertionError):
            check_source_values(inputs[0], corrupted, ASSET)

    def test_crawler_keeps_actual_report_gross_margin_and_cash_fields(self):
        sys.path.insert(0, str(ROOT/'Spider/eastmoney'))
        with patch.dict(sys.modules, {'eastmoney.client': SimpleNamespace(client=object())}):
            mod = importlib.import_module('eastmoney.fetchers.fundamental')
        class Storage:
            def __init__(self): self.rows = {}
            def upsert(self, table, rows): self.rows[table] = rows; return len(rows)
        storage = Storage()
        with patch.object(mod, 'fetch_financials', return_value=[event()['raw_payload']]):
            mod.crawl('1.600909', storage)
        items = {r['item']: r['value'] for r in storage.rows['financials']}
        self.assertEqual(items['毛利率'], 35)
        self.assertEqual(items['每股净资产'], 4)
        self.assertEqual(items['每股经营现金流'], .6)
        self.assertNotIn('资产负债率', items)
        self.assertEqual(len(storage.rows['events']), 1)

    def test_catalog_resolves_distinct_history_and_parsed_report_sources(self):
        from scripts.analytics.data_asset_contracts import DATASETS
        rows = {r[0]: r for r in DATASETS}
        self.assertEqual(len(rows), len(DATASETS))
        self.assertEqual(rows['company.financial_metrics'][3], 'company.financial_metrics')
        self.assertEqual(rows['company.financial_statement_metrics'][3], 'company.financial_statement_metrics')
        self.assertIn('market.financial_events', rows['company.financial_metrics'][6])


if __name__ == '__main__':
    unittest.main()
