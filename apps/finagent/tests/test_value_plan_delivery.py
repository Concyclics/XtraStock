import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'MMClaw'))
from mmclaw.value_plan_delivery import prepare_payload,deliver_after_report

class PlanDeliveryTests(unittest.TestCase):
    def plan(self):
        return dict(stock_code='600000',name='示例',plan_id='internal-id',state='eligible_to_buy',change='added',
            entry_limit=9.5,entry_expires_on='2026-10-16',reason=['估值折价且盈利改善'])
    def test_late_publication_cancels_expired_entry_in_message(self):
        text,plans=prepare_payload([self.plan()],'2026-10-16','2026-10-16T09:16:00+08:00',['2026-10-16','2026-10-19'])
        self.assertEqual(plans[0]['state'],'removed');self.assertNotIn('买入上限',''.join(text))
        self.assertNotIn('internal-id',''.join(text))
    def test_morning_plan_keeps_limit_and_never_claims_target_probability(self):
        text,plans=prepare_payload([self.plan()],'2026-10-16','2026-10-16T09:05:00+08:00',['2026-10-16'])
        self.assertIn('买入上限 9.50',''.join(text));self.assertEqual(plans[0]['first_eligible_session'],'2026-10-16')
        self.assertNotIn('预期收益',''.join(text))
    def test_disabled_delivery_never_calls_network_or_database(self):
        def forbidden(*args):raise AssertionError('unexpected side effect')
        with patch('mmclaw.value_plan_delivery.load_config',return_value={'publish_enabled':False}):
            r=deliver_after_report('test-group','2026-10-16','/unused','2026-10-16T09:05:00+08:00',forbidden,forbidden,forbidden)
        self.assertEqual(r['status'],'disabled')
    def test_no_validation_never_sends(self):
        def forbidden(*args):raise AssertionError('unexpected side effect')
        with patch('mmclaw.value_plan_delivery.load_config',return_value={'publish_enabled':True}),patch(
             'mmclaw.value_plan_delivery.publication_approval',return_value=(False,{})):
            r=deliver_after_report('test-group','2026-10-16','/unused','2026-10-16T09:05:00+08:00',forbidden,forbidden,forbidden)
        self.assertEqual(r['status'],'validation_pending')

if __name__=='__main__':unittest.main()
