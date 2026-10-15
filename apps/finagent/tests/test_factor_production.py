import unittest

from scripts.analytics.evaluate_factors import passed


class FactorProductionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gates = {
            "min_test_dates": 40,
            "min_coverage": 0.60,
            "min_abs_rank_ic": 0.02,
            "min_rank_ic_ir": 0.20,
            "max_q_value": 0.10,
            "min_sign_consistency": 0.55,
            "min_top_bottom_spread": 0.0,
            "max_turnover": 0.50,
        }
        self.metrics = {
            "input_contract_version": "pit-input-v1", "strict_pit_ready": True,
            "date_count": 120,
            "cost_adjusted": True, "execution_constraints": "daily_bar_proxy", "net_spread_mean": 0.005,
            "coverage": 0.90,
            "rank_ic_mean": 0.04,
            "rank_ic_ir": 0.45,
            "sign_consistency": 0.62,
            "top_bottom_spread_mean": 0.01,
            "turnover_mean": 0.25,
        }

    def test_complete_positive_evidence_passes(self) -> None:
        self.assertTrue(passed(self.metrics, 0.03, self.gates))

    def test_negative_long_short_spread_is_rejected(self) -> None:
        metrics = {**self.metrics, "top_bottom_spread_mean": -0.01}
        self.assertFalse(passed(metrics, 0.03, self.gates))

    def test_missing_turnover_is_rejected(self) -> None:
        metrics = {**self.metrics, "turnover_mean": None}
        self.assertFalse(passed(metrics, 0.03, self.gates))

    def test_excessive_turnover_is_rejected(self) -> None:
        metrics = {**self.metrics, "turnover_mean": 0.75}
        self.assertFalse(passed(metrics, 0.03, self.gates))


if __name__ == "__main__":
    unittest.main()
