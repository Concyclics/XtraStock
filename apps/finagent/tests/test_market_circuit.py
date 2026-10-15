from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Spider/eastmoney"))
from eastmoney.circuit import EndpointCircuit
from eastmoney.client import EastMoneyClient


class CircuitTests(unittest.TestCase):
    def test_endpoint_outage_does_not_disable_independent_source(self):
        now = [100.0]
        c = EndpointCircuit(failures=2, cooldown=10, clock=lambda: now[0])
        for _ in range(2):
            self.assertTrue(c.allow("daily")); c.record("daily", False)
        self.assertFalse(c.allow("daily"))
        self.assertTrue(c.allow("fundflow"))
        now[0] += 11
        self.assertTrue(c.allow("daily"))
        self.assertFalse(c.allow("daily"))
        c.record("daily", True)
        self.assertTrue(c.allow("daily"))

    def test_empty_instrument_does_not_trip_transport_circuit(self):
        client = EastMoneyClient()
        with mock.patch.object(client, "_request", return_value={"data": None}):
            for _ in range(5):
                self.assertIsNone(client.get_qt("/api/qt/stock/get", {}))
        self.assertFalse(client.circuit.snapshot()["/api/qt/stock/get"]["open"])

    def test_total_outage_suppresses_later_requests(self):
        client = EastMoneyClient()
        with mock.patch.object(client, "_request", return_value=None) as request:
            for _ in range(30):
                client.get_qt("/api/qt/stock/get", {})
        self.assertLessEqual(request.call_count, 3 * len(client._host_order("/api/qt/stock/get")))
        self.assertEqual(27, client.circuit.snapshot()["/api/qt/stock/get"]["suppressed"])

    def test_expired_batch_budget_never_calls_upstream(self):
        client = EastMoneyClient(); client.deadline = 1
        with mock.patch.object(client, "_request") as request:
            self.assertIsNone(client.get_qt("/api/qt/stock/get", {}))
        request.assert_not_called()

    def test_fundflow_and_daily_use_different_host_health_keys(self):
        c = EastMoneyClient()
        self.assertNotEqual(c._endpoint_key("/api/qt/stock/fflow/daykline/get"), c._endpoint_key("/api/qt/stock/kline/get"))
