import unittest

from mmclaw.evidence import EvidenceStore


FACTOR_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb:E0001"
PRICE_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb:E0002"


class CitedMetadataTests(unittest.TestCase):
    def setUp(self):
        self.store = EvidenceStore("a" * 32, "分析因子和行情")
        self.store.items[FACTOR_ID] = {
            "evidence_id": FACTOR_ID, "claim_type": "model_signal", "metric_id": "momentum_20d",
            "value": {"factor_name": "momentum_20d", "display_name": "20日动量", "percentile": 0.0992},
        }

    def test_cited_factor_name_is_not_an_unsupported_value(self):
        self.assertTrue(self.store.validate(f"20日动量分位数为9.92%。[{FACTOR_ID}]")["passed"])
        self.assertTrue(self.store.validate(f"momentum_20d分位数为9.92%。[{FACTOR_ID}]")["passed"])

    def test_horizon_does_not_authorize_a_score_or_return_of_same_number(self):
        for text in ("20日动量分位数20%。", "20日动量得分20。", "120日动量分位数9.92%。"):
            with self.subTest(text=text):
                self.assertFalse(self.store.validate(f"{text}[{FACTOR_ID}]")["passed"])

    def test_other_paragraph_or_unknown_citation_cannot_authorize_factor_name(self):
        self.assertFalse(self.store.validate(f"分位数9.92%。[{FACTOR_ID}]\n\n20日动量未引用出处。")["passed"])
        self.assertFalse(self.store.validate(f"20日动量分位数9.92%。[{PRICE_ID}]")["passed"])

    def test_descriptive_ohlc_units_only_authorize_prices(self):
        self.store.items[PRICE_ID] = {
            "evidence_id": PRICE_ID, "claim_type": "market", "unit": "CNY; volume/amount source units retained",
            "value": {"open": "325.00", "high": "329.50", "low": "311.42", "close": "326.46",
                      "volume": "334512.82", "amount": "123456"},
        }
        self.assertTrue(self.store.validate(f"开盘325.00元，收盘326.46元。[{PRICE_ID}]")["passed"])
        for value in ("326.46万元", "326.48元", "334512.82元", "123456元"):
            self.assertFalse(self.store.validate(f"收盘{value}。[{PRICE_ID}]")["passed"])


if __name__ == "__main__":
    unittest.main()
