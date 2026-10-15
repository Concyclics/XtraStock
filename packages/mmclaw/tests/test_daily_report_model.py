"""Daily briefs use Astra/high without changing interactive conversations."""
from copy import deepcopy
import io
import unittest
from unittest import mock
import urllib.error

from mmclaw.connectors import daily_report_engine_config, QQBotConnector, WeChatConnector
from mmclaw.providers.openai_compatible import OpenAICompatibleProvider


class DailyReportModelTests(unittest.TestCase):
    def config(self):
        return {'engine_type': 'openai_compatible_sub2api',
                'engines': {'openai_compatible_sub2api': {'model': 'gpt-6-luna', 'reasoning_effort': 'high',
                    'api_key': 'test-key', 'base_url': 'https://provider.invalid/v1', 'api_mode': 'responses', 'timeout': 120}},
                'provider_fallbacks': ['other-model'],
                'daily_report_generation': {'model': 'gpt-6-astra', 'reasoning_effort': 'high', 'timeout': 240}}

    def test_daily_override_is_isolated_and_cannot_fall_back_to_interactive_model(self):
        config = self.config()
        before = deepcopy(config)
        selected = daily_report_engine_config(config)
        engine = selected['engines'][selected['engine_type']]
        self.assertEqual(engine['model'], 'gpt-6-astra')
        self.assertEqual(engine['reasoning_effort'], 'high')
        self.assertEqual(engine['timeout'], 240)
        self.assertTrue(engine['require_reasoning_effort'])
        self.assertEqual(engine['api_key'], 'test-key')
        self.assertEqual(selected['provider_fallbacks'], [])
        self.assertEqual(config, before)

    def test_unconfigured_workspaces_keep_existing_routing(self):
        config = self.config()
        del config['daily_report_generation']
        self.assertEqual(daily_report_engine_config(config), config)

    def test_qq_and_wechat_brief_calls_use_daily_override(self):
        for connector_type in (QQBotConnector, WeChatConnector):
            with self.subTest(connector=connector_type.__name__), \
                 mock.patch('mmclaw.connectors.render_report_file', return_value='真实日报'), \
                 mock.patch('mmclaw.connectors.Engine') as engine:
                connector = connector_type.__new__(connector_type)
                connector.config = self.config()
                connector._bool_config = lambda name, default: default
                connector._int_config = lambda name, default: default
                connector._today_key = lambda: '2026-10-13'
                connector._log_event = mock.Mock()
                engine.return_value.ask.return_value = {'content': '结论：关注已披露业绩。'}
                brief = connector._daily_report_brief_message('/local/preview.md')
                self.assertIn('结论', brief)
                selected = engine.call_args.args[0]
                self.assertEqual(selected['engines'][selected['engine_type']]['model'], 'gpt-6-astra')
                self.assertEqual(connector.config['engines'][selected['engine_type']]['model'], 'gpt-6-luna')
                self.assertIsNone(engine.return_value.ask.call_args.kwargs['tools'])
                connector._log_event.assert_not_called()

    def test_summary_provider_never_retries_without_required_reasoning(self):
        provider = OpenAICompatibleProvider(daily_report_engine_config(self.config()))
        with mock.patch.object(provider, '_ask_responses', side_effect=urllib.error.HTTPError(
                'https://provider.invalid', 400, 'bad reasoning', {}, io.BytesIO(b'bad reasoning'))) as ask:
            with self.assertRaises(urllib.error.HTTPError):
                provider.ask_once([{'role': 'user', 'content': 'summarize'}])
        ask.assert_called_once()
        self.assertEqual(ask.call_args.args[1]['reasoning'], {'effort': 'high'})


if __name__ == '__main__':
    unittest.main()
