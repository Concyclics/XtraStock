import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/analytics'))
spec = importlib.util.spec_from_file_location('corporate_formula', ROOT / 'scripts/analytics/extract_corporate_actions_cninfo.py')
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


class FormulaTests(unittest.TestCase):
    def test_diluted_shares_from_explicit_reference_price_denominator(self):
        r=parser.parse_distribution('每10股派5元，每10股以资本公积转增股本4股。'
          '本次权益分派实施后除权除息参考价=（除权除息日前一日收盘价-0.4805563元）/(1+0.3844451)。'
          '股权登记日：2026/6/5，除权除息日：2026/6/8。')
        self.assertAlmostEqual(r['split_ratio'],.3844451);self.assertEqual(r['declared_split_ratio'],.4)

    def test_payment_before_entitlement_is_unknown_not_early_cash(self):
        r=parser.parse_distribution('每10股派3元。股权登记日：2021/7/8，除权除息日：2021/7/9，现金红利发放日：2020/7/9。')
        self.assertIsNone(r['pay_date']);self.assertEqual(r['quality_status'],'unverified')

    def test_unchanged_current_approved_plan_can_supply_omitted_transfer(self):
        r=parser.parse_distribution('一、股东大会审议通过的方案：每10股派4元，每10股转增4股。'
          '公司股本总额未发生变化。本次实施的分配方案与股东大会审议通过的方案一致。'
          '二、本次实施的利润分配方案：每10股派4元。三、股权登记日：2024/5/23，除权除息日：2024/5/24。')
        self.assertEqual(r['split_ratio'],.4)

    def test_current_plan_cash_and_transfer_wording_variants(self):
        for cash in ['每股派发的现金红利为0.15元','每股派发现金红利0.15，共计派发现金红利150000元。元（含税）']:
            r=parser.parse_distribution('一、分配方案：'+cash+'。二、股权登记日：2023/7/17，除息日：2023/7/18。')
            self.assertEqual(r['cash_dividend'],.15)
        r=parser.parse_distribution('二、本次权益分派方案：每10股转增股4股，不进行现金分红。三、股权登记日：2023/9/27，除权除息日：2023/9/28。')
        self.assertEqual(r['split_ratio'],.4)

    def test_send_shares_and_cash_in_same_clause_precedes_tax_example(self):
        r=parser.parse_distribution('二、本次实施的权益分派方案：每10股送股2股，派10元人民币现金（含税，扣税后每10股派8.8元）；每10股转增3股。'
          '三、股权登记日：2024/6/6，除权除息日：2024/6/7。')
        self.assertEqual(r['cash_dividend'],1.);self.assertEqual(r['stock_dividend_ratio'],.2);self.assertEqual(r['split_ratio'],.3)

    def test_historical_examples_do_not_create_current_share_entitlements(self):
        r=parser.parse_distribution('二、本次实施的利润分配方案：每10股派3元。不以公积金转增股本。'
          '三、股权登记日与除权除息日。股权登记日：2021/6/21，除权除息日：2021/6/22。'
          '六、调整相关参数：2009年度每10股送红股2股，每10股转增5股，每10股派4元。')
        self.assertEqual(r['cash_dividend'],.3);self.assertIsNone(r['split_ratio']);self.assertIsNone(r['stock_dividend_ratio'])

    def test_current_share_plan_survives_later_historical_examples(self):
        r=parser.parse_distribution('二、本次权益分派方案：每10股派2元，每10股转增3股。'
          '三、股权登记日：2025/6/16，除权除息日：2025/6/17。'
          '六、相关参数调整情况：2018年每10股转增6股。')
        self.assertEqual(r['split_ratio'],.3);self.assertEqual(r['cash_dividend'],.2)

    def parse(self, formula, split='0.4', extra=''):
        return parser.parse_distribution(
            f'差异化分红送转：是。每股现金红利1.2元，每股转增{split}股。'
            '虚拟分派的每股现金红利=(119729530*1.2)/120045559≈1.1968元。'
            + extra + formula + '。股权登记日：2025/7/1，除权（息）日：2025/7/2。')

    def test_comma_separated_numerator_is_not_ratio(self):
        r = self.parse('虚拟分派的流通股份变动比例=（参与分配的股本总数×实际分派的送转比例）÷总股本=119,729,530*0.4/120,045,559=0.3989（保留四位小数）')
        self.assertEqual(r['split_ratio'], .3989)
        self.assertEqual(r['declared_split_ratio'], .4)
        self.assertEqual(r['declared_cash_dividend'], 1.2)

    def test_prefer_last_rounded_completed_result(self):
        r = self.parse('虚拟分派的流通股份变动比例=991,415,391×0.3÷1,012,038,353=0.29388671≈0.2939', split='0.3')
        self.assertEqual(r['split_ratio'], .2939)

    def test_do_not_cross_cash_clause(self):
        r = self.parse('虚拟分派的流通股份变动比例以实际分派摊薄调整后计算得出：虚拟分派的每股现金红利=(82541884*3.6)/83317500≈3.5665元/股虚拟分派的流通股份变动比例=(82541884*0.49)/83317500≈0.4854根据虚拟分派计算的除权参考价格', split='0.49')
        self.assertEqual(r['split_ratio'], .4854)

    def test_incomplete_formula_not_treated_as_verified(self):
        r = self.parse('虚拟分派的流通股份变动比例=119,729,530*0.4/120,045,559')
        self.assertEqual(r['split_ratio'], .4)
        self.assertFalse(r['effective_share_change_parsed'])
        self.assertEqual(r['quality_status'], 'unverified')

    def test_no_double_count_when_split_and_stock_dividend_coexist(self):
        r = self.parse('虚拟分派的流通股份变动比例=0.49', split='0.4', extra='每股送红股0.1股。')
        self.assertAlmostEqual(r['split_ratio'] + r['stock_dividend_ratio'], .49)

    def test_direct_statement_and_impossible_ratio(self):
        self.assertEqual(parser.effective_share_ratio('流通股份变动比例为0.3989。', .4), .3989)
        self.assertIsNone(parser.effective_share_ratio('流通股份变动比例=119。', .4))

    def test_virtual_ratio_takes_priority_over_nominal_description(self):
        r = self.parse('流通股份变动比例为0.4。虚拟分派的流通股份变动比例=(100*0.4)/102=0.3920')
        self.assertEqual(r['split_ratio'], .392)

    def test_adjacent_cash_formula_without_punctuation(self):
        r = self.parse('虚拟分派的流通股份变动比例=(175543435*0.4)/175543467≈0.399999（保留六位）现金红利＝(175543435*0.08938)/175543467≈0.08938元/股')
        self.assertEqual(r['split_ratio'], .399999)

    def test_direct_cash_formula_does_not_scan_to_conversion_price(self):
        r=parser.parse_distribution('每10股派1.5000000元人民币现金。即每股现金红利=0.1489440元/股。'
            '转股价格=10.3元。股权登记日：2023/6/7，除权除息日：2023/6/8。')
        self.assertAlmostEqual(r['cash_dividend'],.148944)

    def test_cash_reference_subtraction_not_future_bond_price(self):
        r=parser.parse_distribution('每10股派1.500000元人民币现金。'
            '按总股本折算每股现金红利=除权除息日前一日收盘价-0.1488939元。'
            '转股价格=25.0元。股权登记日：2024/5/14，除权除息日：2024/5/15。')
        self.assertAlmostEqual(r['cash_dividend'],.1488939)

    def test_missing_ex_label_variant_and_parenthesized_pay_date(self):
        r=parser.parse_distribution('每10股派发现金红利3.90元；每10股转增6.00股。'
            '股权登记日为：2022年5月30日。本次权益分派除权（除息）日为：2022年5月31日。'
            '新增可流通股份上市日（红利发放日）为：2022年5月31日。')
        self.assertEqual(str(r['ex_date']),'2022-05-31');self.assertEqual(str(r['pay_date']),'2022-05-31')

    def test_multiline_shared_ex_date_in_aligned_table(self):
        text=('每股现金红利2元，每股转增0.4股。\n'
              '     股份类别     股权登记日        最后交易日       除权（息）日       条件流通股\n'
              '                                                                 放日\n'
              '                                                     份上市日\n'
              '      Ａ股       2022/6/10     －                      2022/6/14   2022/6/13\n'
              '                                        2022/6/13\n'
              '      Ｂ股       2022/6/15   2022/6/10                2022/6/17   2022/6/23\n')
        r=parser.parse_distribution(text)
        self.assertEqual(str(r['ex_date']),'2022-06-13');self.assertEqual(str(r['pay_date']),'2022-06-13')

    def test_gross_cash_precedes_after_tax_example(self):
        t='每10股派16.00元人民币现金（含税）；扣税后QFII每10股派14.40元。每股现金红利应以1.594265元/股计算。'
        self.assertEqual(parser.declared_cash_amount(t),1.6)
        self.assertEqual(parser.effective_cash_ratio(t,1.6),1.594265)

    def test_cash_formula_with_final_ji_scalar(self):
        self.assertEqual(parser.effective_cash_ratio('每股现金红利=实际现金分红总金额÷总股本,即0.094621元/股=238,803,276.9元÷2,523,777,297股。',.1),.094621)

    def test_cash_only_per_ten_format(self):
        self.assertEqual(parser.declared_cash_amount('每10股现金分红0.25元（含税）。每10股派0.225元；'),.025)

    def test_per_share_clause_does_not_absorb_per_ten_clause(self):
        t='每股现金红利=127,148,042.70元÷869,230,324股=0.1462766元/股，按总股本折算的每10股现金分红=0.146277×10=1.462766元。'
        self.assertAlmostEqual(parser.effective_cash_ratio(t,.15),127148042.7/869230324)

    def test_numeric_operands_correct_inconsistent_printed_unit(self):
        t='每股现金红利=现金分红总额/总股本，即0.399430元/股=33,381,417.23元/835,724,374股。'
        self.assertAlmostEqual(parser.effective_cash_ratio(t,.041),33381417.23/835724374)

    def test_reference_formula_unicode_minus_and_missing_currency(self):
        t=parser.normalize_text('除权除息参考价=股权登记日收盘价‒0.4934766元÷10股=股权登记日收盘价‒0.04934766元。')
        self.assertAlmostEqual(parser.effective_cash_ratio(t,.05),.04934766)
        self.assertAlmostEqual(parser.effective_cash_ratio('除权除息价格=收盘价-0.0496948。',.05),.0496948)

    def test_parenthesized_tax_and_bse_formula(self):
        self.assertAlmostEqual(parser.effective_cash_ratio('每股现金红利（含税）=62,465,088.90元÷430,973,206股=0.1449396元/股。',.15),.1449396)
        self.assertAlmostEqual(parser.effective_cash_ratio('按总股本折算的每股现金红利=135187776×（每10股分红金额÷10）÷总股本=135187776×（2.5÷10）÷139497776=0.2422759元；',.25),.2422759)

    def test_special_dividend_total_and_missing_share_suffix(self):
        self.assertAlmostEqual(parser.effective_cash_ratio('每股现金红利=6,713,581,875.00元÷4,816,186,983股=1.3939620元/股。',.4),1.393962)
        self.assertAlmostEqual(parser.effective_cash_ratio('每股现金红利=3,478,346.39元÷347,834,639=0.100000元/股。',.0100314),.01)

    def test_long_formula_is_not_truncated_inside_decimal(self):
        t='每股现金红利='+('说明'*140)+'=0.114205元/股流通股份变动比例=0。'
        self.assertEqual(parser.effective_cash_ratio(t,.115),.114205)

    def test_reference_with_units_in_heading(self):
        t='除权除息参考价格（元/股）=收盘价-按公司总股本折算每股现金红利=收盘价-0.2277088。'
        self.assertEqual(parser.effective_cash_ratio(t,.228),.2277088)

    def test_symbolic_template_does_not_hide_following_numeric_formula(self):
        t='每股现金红利=参与分配的股本总数×实际分派的每股现金红利÷总股本流通股份变动比例=总股本比如果按总股本计算，每股现金红利=9695000000×0.8÷10000000000=0.7756元/股。'
        self.assertEqual(parser.effective_cash_ratio(t,.8),.7756)


if __name__ == '__main__':
    unittest.main()
