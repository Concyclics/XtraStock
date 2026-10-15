"""Daily generation must keep its requested reasoning budget on all attempts."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/pipeline'))
import openai_compatible_client as client
import daily_ai_hardware_report_v3 as daily


class Response:
    def __init__(self, body=None, events=()):
        self.body = body
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.body).encode()

    def __iter__(self):
        return iter(('data: ' + json.dumps(event) + '\n').encode() for event in self.events)


def completed_body(text='complete'):
    return {'model': 'gpt-6-astra', 'status': 'completed', 'reasoning': {'effort': 'high'},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 50,
                      'output_tokens_details': {'reasoning_tokens': 20}}}


class DailyReasoningContractTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'TEST_DAILY_KEY': 'secret-test-key', 'FINANCE_API_MIN_RETRIES': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def complete(self, **kwargs):
        return client.complete([{'role': 'user', 'content': 'private-prompt'}], model='gpt-6-astra',
            base_url='https://provider.invalid/v1', api_key_env='TEST_DAILY_KEY',
            reasoning_effort='high', api_mode='responses', **kwargs)

    def test_parameter_error_never_drops_high(self):
        with mock.patch.object(client.urllib.request, 'urlopen', side_effect=urllib.error.HTTPError(
                'https://provider.invalid', 400, 'unsupported reasoning', {}, io.BytesIO(b'bad parameter'))) as opened:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 400'):
                self.complete(stream=True, retries=2)
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(json.loads(opened.call_args.args[0].data)['reasoning'], {'effort': 'high'})

    def test_transient_retry_preserves_budget_and_secret_safe_audit(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(client.time, 'sleep'), \
             mock.patch.object(client.urllib.request, 'urlopen', side_effect=[
                 urllib.error.HTTPError('https://provider.invalid', 503, 'busy', {}, io.BytesIO(b'busy')),
                 Response(completed_body())]) as opened:
            audit_path = Path(directory) / 'audit.json'
            self.assertEqual(self.complete(retries=1, audit_path=audit_path), 'complete')
            for call in opened.call_args_list:
                payload = json.loads(call.args[0].data)
                self.assertEqual(payload['model'], 'gpt-6-astra')
                self.assertEqual(payload['reasoning'], {'effort': 'high'})
            audit = json.loads(audit_path.read_text())
            self.assertTrue(audit['completed'])
            self.assertEqual(audit['attempts'][-1]['usage']['reasoning_tokens'], 20)
            self.assertNotIn('secret-test-key', audit_path.read_text())
            self.assertNotIn('private-prompt', audit_path.read_text())

    def test_complete_stream_records_returned_model_and_effort(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(client.urllib.request, 'urlopen', return_value=Response(events=[
                 {'type': 'response.output_text.delta', 'delta': 'complete'},
                 {'type': 'response.completed', 'response': completed_body()}])) as opened:
            audit_path = Path(directory) / 'audit.json'
            self.assertEqual(self.complete(stream=True, retries=0, audit_path=audit_path), 'complete')
            attempt = json.loads(audit_path.read_text())['attempts'][0]
            self.assertEqual(attempt['response_model'], 'gpt-6-astra')
            self.assertEqual(attempt['response_reasoning_effort'], 'high')
            opened.assert_called_once()

    def test_truncated_stream_retries_instead_of_accepting_partial_text(self):
        with mock.patch.object(client.urllib.request, 'urlopen', side_effect=[
                Response(events=[{'type': 'response.output_text.delta', 'delta': 'partial'}]),
                Response(completed_body())]) as opened:
            self.assertEqual(self.complete(stream=True, retries=0), 'complete')
        self.assertEqual(opened.call_count, 2)
        self.assertTrue(all(json.loads(c.args[0].data)['reasoning'] == {'effort': 'high'} for c in opened.call_args_list))

    def test_incomplete_blocking_result_is_not_publishable(self):
        body = completed_body('partial')
        body['status'] = 'incomplete'
        with mock.patch.object(client.urllib.request, 'urlopen', return_value=Response(body)):
            with self.assertRaisesRegex(RuntimeError, 'not completed'):
                self.complete(retries=0)

    def test_codex_route_receives_explicit_high_budget(self):
        proc = mock.Mock(returncode=0)
        proc.communicate.return_value = ('', None)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(daily.subprocess, 'Popen', return_value=proc) as popen:
            daily.run_codex(Path(directory), 'test', 'gpt-6-astra', 60, 'high')
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index('--model') + 1], 'gpt-6-astra')
        self.assertIn('model_reasoning_effort="high"', command)


if __name__ == '__main__':
    unittest.main()
