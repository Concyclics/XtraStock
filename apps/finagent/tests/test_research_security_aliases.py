import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/analytics'))
from import_research_security_aliases import parse_mapping
from backfill_dated_forecast_history import extract_body, months, pdf_identity_supported


class SecurityAliasTest(unittest.TestCase):
    def test_official_collision_mapping_and_pilot_date(self):
        body = '<table><tr><td>1</td><td>芭薇股份</td><td>2024/3/29</td><td>837023</td><td>920123</td></tr>'
        body += '<tr><td>2</td><td>佳先股份</td><td>2020/7/27</td><td>430489</td><td>920489</td></tr></table>'
        rows = parse_mapping(body.encode())
        self.assertEqual(rows['837023']['new_code'], '920123')
        self.assertEqual(rows['837023']['switch_date'], '2025-10-09')
        self.assertEqual(rows['430489']['switch_date'], '2025-05-06')

    def test_ambiguous_source_is_rejected(self):
        row = '<tr><td>1</td><td>佳先股份</td><td>2020/7/27</td><td>430489</td><td>920489</td></tr>'
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            parse_mapping(('<table>' + row * 2 + '</table>').encode())

    def test_empty_mapping_rejected(self):
        with self.assertRaisesRegex(ValueError, 'No official'):
            parse_mapping(b'<html>access denied</html>')


class HistoricalBodyTest(unittest.TestCase):
    def test_html_identity_is_bound(self):
        page = '<html><title>dated A</title><div id="ctx-content"><p>' + 'x' * 100 + '</p></div></html>'
        self.assertEqual(len(extract_body(page, 'dated A')[0]), 100)
        with self.assertRaisesRegex(ValueError, 'title'):
            extract_body(page, 'dated B')

    def test_missing_body_not_a_success(self):
        with self.assertRaises(ValueError):
            extract_body('<title>A</title><div id="ctx-content">download PDF</div>', 'A')

    def test_month_partitions_no_gap_or_overlap(self):
        self.assertEqual(list(months('2020-02-28', '2020-03-02')),
                         [('2020-02-28', '2020-02-29'), ('2020-03-01', '2020-03-02')])

    def test_issuer_name_and_complete_pdf_title_must_both_match(self):
        text = '万孚生物\n产品全面销售强力 POCT 龙头出列'
        self.assertEqual(pdf_identity_supported(text, '300482', '万孚生物', '产品全面销售强力 POCT龙头出列'),
                         'exact_issuer_name_and_report_title_on_first_two_pages')
        with self.assertRaises(ValueError):
            pdf_identity_supported(text, '600000', '浦发银行', '产品全面销售强力 POCT龙头出列')
        with self.assertRaises(ValueError):
            pdf_identity_supported(text, '300482', '万孚生物', '完全不同的报告')


if __name__ == '__main__':
    unittest.main()
