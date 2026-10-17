import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

path = Path(__file__).resolve().parents[1] / 'apps/finagent/inference_http.py'
spec = importlib.util.spec_from_file_location('inference_http', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CapacityWaitTests(unittest.TestCase):
    def test_capacity_wait_does_not_retry_unrelated_errors(self):
        for code, headers in [(400, {}), (500, {}), (503, {})]:
            session = Mock()
            session.post.return_value = Mock(status_code=code, headers=headers)
            self.assertIs(module.post_with_capacity_wait(session, 'http://localhost'), session.post.return_value)
            session.post.assert_called_once()

    def test_marked_capacity_response_waits_then_returns_success(self):
        busy = Mock(status_code=503, headers={'x-finagent-retryable': 'capacity', 'Retry-After': '1'})
        good = Mock(status_code=200)
        session = Mock(); session.post.side_effect = [busy, good]
        with patch.object(module.time, 'monotonic', side_effect=[0, 2]), patch.object(module.time, 'sleep'):
            self.assertIs(module.post_with_capacity_wait(session, 'http://localhost'), good)
        busy.close.assert_called_once()
        self.assertEqual(session.post.call_count, 2)

    def test_stop_request_and_expired_deadline_do_not_send_requests(self):
        session = Mock()
        with self.assertRaises(requests.Timeout):
            module.post_with_capacity_wait(session, 'http://localhost', stop_requested=lambda: True)
        with self.assertRaises(requests.Timeout):
            module.post_with_capacity_wait(session, 'http://localhost', deadline=-1)
        session.post.assert_not_called()

    def test_deadline_bounds_http_timeout(self):
        session = Mock();session.post.return_value = Mock(status_code=200)
        with patch.object(module.time, 'monotonic', return_value=10):
            module.post_with_capacity_wait(session, 'http://localhost', deadline=15)
        self.assertEqual(session.post.call_args.kwargs['timeout'], 5)
