"""Daily polling must not regenerate summaries or resend completed deliveries."""
import unittest
from unittest import mock

from mmclaw.connectors import QQBotConnector


class QQDailyDedupTests(unittest.TestCase):
    def connector(self, state, groups=("group-a", "group-b")):
        connector = QQBotConnector.__new__(QQBotConnector)
        connector._daily_report_due = mock.Mock(return_value=True)
        connector._today_key = mock.Mock(return_value="2026-10-13")
        connector._now_iso = mock.Mock(return_value="2026-10-13T01:05:00+00:00")
        connector._load_daily_report_state = mock.Mock(return_value=state)
        connector._save_daily_report_state = mock.Mock()
        connector._daily_report_group_openids = mock.Mock(return_value=list(groups))
        connector._find_daily_report = mock.Mock(return_value="/reports/2026-10-13.md")
        connector._daily_report_messages = mock.Mock(return_value=["full report"])
        connector._daily_report_brief_message = mock.Mock(return_value="brief")
        connector.send_messages_to_group = mock.Mock(return_value=True)
        connector._log_event = mock.Mock()
        connector._maybe_push_value_plan = mock.Mock()
        return connector

    def assert_no_generation_or_send(self, connector):
        connector._find_daily_report.assert_not_called()
        connector._daily_report_messages.assert_not_called()
        connector._daily_report_brief_message.assert_not_called()
        connector.send_messages_to_group.assert_not_called()

    def test_already_sent_groups_do_not_call_model_on_each_poll(self):
        connector = self.connector({"sent_groups": {
            group: {"last_sent_date": "2026-10-13"} for group in ("group-a", "group-b")
        }})
        for _ in range(3):
            connector._maybe_push_daily_report()
        self.assert_no_generation_or_send(connector)
        connector._save_daily_report_state.assert_not_called()

    def test_legacy_delivery_is_also_deduplicated_before_generation(self):
        connector = self.connector({"last_sent_date": "2026-10-13", "group_openid": "group-a"},
                                   groups=("group-a",))
        connector._maybe_push_daily_report()
        self.assert_no_generation_or_send(connector)

    def test_failed_group_keeps_existing_no_automatic_retry_policy(self):
        connector = self.connector({"sent_groups": {
            "group-a": {"last_sent_date": "2026-10-13"},
            "group-b": {"last_failed_date": "2026-10-13"},
        }})
        connector._maybe_push_daily_report()
        self.assert_no_generation_or_send(connector)

    def test_only_unsent_group_is_delivered_and_next_poll_is_idle(self):
        connector = self.connector({"sent_groups": {
            "group-a": {"last_sent_date": "2026-10-13"},
        }})
        connector._maybe_push_daily_report()
        connector._maybe_push_daily_report()
        connector._daily_report_brief_message.assert_not_called()
        connector.send_messages_to_group.assert_called_once_with("group-b", ["full report"])
        self.assertEqual("2026-10-13", connector._load_daily_report_state.return_value[
            "sent_groups"]["group-b"]["last_sent_date"])

    def test_previous_day_does_not_block_new_day_and_full_report_is_shared(self):
        connector = self.connector({"sent_groups": {
            "group-a": {"last_sent_date": "2026-10-12"},
            "group-b": {"last_failed_date": "2026-10-12"},
        }})
        connector._maybe_push_daily_report()
        connector._maybe_push_daily_report()
        connector._daily_report_brief_message.assert_not_called()
        self.assertEqual(connector.send_messages_to_group.call_args_list, [
            mock.call("group-a", ["full report"]),
            mock.call("group-b", ["full report"]),
        ])


if __name__ == "__main__":
    unittest.main()
