"""Delivery sends the validated report once and never generates another brief."""
import io
import asyncio
import unittest
from unittest import mock
import urllib.error

from mmclaw.connectors import QQBotConnector, WeChatConnector
from mmclaw.providers.openai_compatible import OpenAICompatibleProvider


class DailyReportModelTests(unittest.TestCase):
    def test_qq_full_report_still_reaches_the_existing_async_sender(self):
        c=self.connector(QQBotConnector)
        c._api=mock.Mock(post_group_message=mock.AsyncMock(),post_c2c_message=mock.AsyncMock())
        c._msg_seq={}
        self.assertTrue(asyncio.run(c._send_target_async('group','test-group','full report')))
        c._api.post_group_message.assert_awaited_once()
        self.assertEqual(c._api.post_group_message.call_args.kwargs['content'],'full report')
        c._api.post_c2c_message.assert_not_awaited()

    def connector(self, kind):
        c=kind.__new__(kind)
        # Retired keys in existing private workspaces must not re-enable a call.
        c.config={'daily_report_generation':{'model':'gpt-6-astra','reasoning_effort':'high'}}
        c._bool_config=lambda key,default: True
        c._int_config=lambda key,default: default
        c._today_key=lambda:'2026-10-16'
        c._now_iso=lambda:'2026-10-16T09:05:00+08:00'
        c._log_event=mock.Mock()
        return c

    def test_both_channels_send_only_full_report_without_model_call(self):
        for kind in (QQBotConnector, WeChatConnector):
            with self.subTest(channel=kind.__name__), mock.patch('mmclaw.connectors.Engine') as engine, \
                 mock.patch('mmclaw.connectors.render_report_file',return_value='市场热点与跨行业估值观察。'):
                c=self.connector(kind)
                messages=c._daily_report_messages('/preview.md')
                self.assertEqual(len(messages),1)
                self.assertIn('全市场日报',messages[0]);self.assertNotIn('省流版',messages[0])
                self.assertIn('市场热点与跨行业估值观察',messages[0]);engine.assert_not_called()

    def test_wechat_manual_and_scheduled_delivery_share_single_report(self):
        with mock.patch('mmclaw.connectors.Engine') as engine:
            c=self.connector(WeChatConnector)
            c._find_daily_report=mock.Mock(return_value='/reports/2026-10-16.md')
            c._daily_report_messages=mock.Mock(return_value=['full report part 1','full report part 2'])
            c.send_messages_to_user=mock.Mock(return_value=True)
            c._handle_daily_report_command('reader')
            c.send_messages_to_user.assert_called_once_with('reader',['full report part 1','full report part 2'])
            c.send_messages_to_user.reset_mock()
            c._daily_report_due=lambda:True
            c._daily_report_user_ids=lambda:['subscriber']
            c._load_daily_report_state=mock.Mock(return_value={})
            c._save_daily_report_state=mock.Mock()
            c._maybe_push_daily_report();c._maybe_push_daily_report()
            c.send_messages_to_user.assert_called_once_with('subscriber',['full report part 1','full report part 2'])
            self.assertFalse(c._load_daily_report_state.return_value['sent_users']['subscriber']['brief_sent'])
            engine.assert_not_called()

    def test_required_reasoning_is_not_dropped_after_parameter_error(self):
        cfg={'engine_type':'openai_compatible_sub2api','engines':{'openai_compatible_sub2api':{
            'model':'gpt-6-astra','reasoning_effort':'high','require_reasoning_effort':True,
            'api_key':'test-key','base_url':'https://provider.invalid/v1','api_mode':'responses'}},'provider_fallbacks':[]}
        provider=OpenAICompatibleProvider(cfg)
        with mock.patch.object(provider,'_ask_responses',side_effect=urllib.error.HTTPError(
                'https://provider.invalid',400,'bad reasoning',{},io.BytesIO(b'bad reasoning'))) as ask:
            with self.assertRaises(urllib.error.HTTPError):
                provider.ask_once([{'role':'user','content':'daily report'}])
        ask.assert_called_once()
        self.assertEqual(ask.call_args.args[1]['reasoning'],{'effort':'high'})


if __name__=='__main__':unittest.main()
