import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/analytics'))
from forecast_semantics import extract_dated_forecasts, api_years


class ForecastSemanticsTest(unittest.TestCase):
    def parse(self, text, year=2024):
        rows, rejected = extract_dated_forecasts(text, year)
        return {(r['forecast_year'], r['field']): r for r in rows}, rejected

    def test_explicit_years_override_publication_year(self):
        rows, _ = self.parse('预计2024-2026年公司归母净利为213.52/229.63/239.19亿元，对应EPS分别为2.20/2.37/2.47元，对应PE分别为11/10/10倍。')
        self.assertEqual(rows[2024, 'eps']['value'], '2.20')
        self.assertEqual(rows[2026, 'eps']['value'], '2.47')
        self.assertEqual(float(rows[2024, 'net_profit']['value']), 21352000000)
        self.assertEqual(rows[2024, 'eps']['unit'], 'CNY/share')
        self.assertEqual(rows[2024, 'net_profit']['unit'], 'CNY')

    def test_old_estimates_are_not_current_estimates(self):
        rows, _ = self.parse('预计2024-2026年EPS为2/3/4元（原为3/4/5元），对应PE为20/15/10倍。')
        self.assertEqual(rows[2024, 'eps']['value'], '2')

    def test_source_typo_is_quarantined_not_silently_corrected(self):
        rows, rejected = self.parse('预计2024-2026年EPS为0.53/0.76/10.87元，对应PE为25.4/17.6/15.5倍。')
        self.assertEqual(rows, {})
        self.assertEqual(rejected[-1]['reason'], 'inconsistent_implied_price_or_shares')

    def test_each_pe_value_may_have_its_own_multiple_suffix(self):
        rows, rejected = self.parse('预计2021-2023年EPS分别为1.26/1.57/1.94元，对应PE分别为52X、41X、34X。',2021)
        self.assertFalse(rejected)
        self.assertEqual(rows[2021,'pe']['value'],'52')
        self.assertEqual(rows[2023,'pe']['value'],'34')
        self.assertEqual(rows[2023,'eps']['value'],'1.94')

    def test_per_value_pe_units_cannot_hide_original_eps_typo(self):
        rows, rejected = self.parse('预计2021-2023年EPS分别为1.26/1.57/19.4元，对应PE分别为52X、41X、34X。',2021)
        self.assertEqual(rows,{})
        self.assertEqual(rejected[-1]['reason'],'inconsistent_implied_price_or_shares')

    def test_pe_without_multiple_units_remains_untrusted(self):
        rows, _ = self.parse('预计2021-2023年PE分别为52/41/34。',2021)
        self.assertEqual(rows,{})

    def test_years_are_not_inferred(self):
        self.assertEqual(self.parse('预计EPS分别为2/3/4元。')[0], {})

    def test_cross_paragraph_years_are_not_carried(self):
        self.assertEqual(self.parse('预计2024-2026年营收增长。EPS为2/3/4元。')[0], {})

    def test_actual_results_are_not_forecasts(self):
        self.assertEqual(self.parse('公司2022-2024年EPS分别为2/3/4元。')[0], {})

    def test_growth_percentages_are_not_eps(self):
        self.assertEqual(self.parse('预计2024-2026年EPS增长为20/30/40%。')[0], {})

    def test_count_mismatch_is_rejected(self):
        rows, rejected = self.parse('预计2024-2026年EPS为2/3元。')
        self.assertEqual(rows, {})
        self.assertEqual(rejected[0]['reason'], 'year_value_count_mismatch')

    def test_conflicts_do_not_choose_last(self):
        rows, rejected = self.parse('预计2024-2026年EPS为2/3/4元。预计2024-2026年EPS为3/4/5元。')
        self.assertEqual(rows, {})
        self.assertEqual(len(rejected), 3)

    def test_short_year_list(self):
        rows, _ = self.parse('预计24/25/26年每股收益为1.1、1.2、1.3元。')
        self.assertEqual(rows[2025, 'eps']['value'], '1.2')

    def test_non_consecutive_years_are_rejected(self):
        self.assertEqual(self.parse('预计2022/2024年EPS为1/2元。')[0], {})

    def test_other_company_profit_is_excluded(self):
        self.assertEqual(self.parse('预计2024-2026年子公司EPS为2/3/4元。')[0], {})

    def test_api_uses_response_year_not_report_year(self):
        self.assertEqual(api_years({'预测当年': '2026', '预测年份口径': 'api.currentYear', '日期':'2024-12-08'}), ('2026','2027','2028'))

    def test_legacy_publish_year_is_untrusted(self):
        self.assertEqual(api_years({'预测当年':'2024.0', '日期':'2024-12-08'}), ('','',''))

    def test_real_crawler_preserves_response_year_and_per_share_value(self):
        from eastmoney_reports_incremental import map_stock_item
        row=map_stock_item({'infoCode':'test','publishDate':'2024-12-08','stockCode':'601225',
                            'predictThisYearEps':2.47},current_year=2026)
        self.assertEqual(row['日期'],'2024-12-08')
        self.assertEqual(row['当年盈利预测'],2.47)
        self.assertEqual(api_years(row),('2026','2027','2028'))
        self.assertEqual(row['2024盈利预测'],'')

    def test_legacy_sqlite_gets_year_provenance_column(self):
        import sqlite3
        from eastmoney_reports_incremental import ensure_sqlite_table,KINDS
        with sqlite3.connect(':memory:') as c:
            c.execute('CREATE TABLE stock_reports ("报告链接" TEXT PRIMARY KEY, "预测当年" TEXT)')
            c.execute('INSERT INTO stock_reports VALUES (?,?)',('legacy','2024'))
            ensure_sqlite_table(c,KINDS['stock'])
            names={r[1] for r in c.execute('PRAGMA table_info(stock_reports)')}
            self.assertIn('预测年份口径',names)
            self.assertEqual(c.execute('SELECT "预测当年","预测年份口径" FROM stock_reports').fetchone(),('2024',None))

    def test_forecast_actual_profit_is_separate_from_eps(self):
        from build_asset_forecast_daily import build_sql
        sql = build_sql()
        self.assertIn('fc.net_profit/aa.value-1', sql)
        self.assertNotIn('fc.eps/aa.value', sql)
        self.assertIn('research.report_forecasts_usable', sql)


if __name__ == '__main__':
    unittest.main()
