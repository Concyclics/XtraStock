import unittest
from mmclaw.prediction_contract import prediction_error
class PredictionContractTests(unittest.TestCase):
 def test_current_price_cannot_be_laundered_into_target(self):
  self.assertIsNotNone(prediction_error('目标价为 100 元',[{'close':100}]))
 def test_blocked_and_missing_values_remain_unavailable(self):
  self.assertIsNone(prediction_error('暂无可靠目标价，5 日模型未通过验证',[]))
  self.assertIsNotNone(prediction_error('预期收益 10%', [{'prediction_eligible':False,'expected_return':.1}]))
 def test_approved_scenario_and_attributed_broker_target(self):
  self.assertIsNone(prediction_error('模型目标价为 100',[{'prediction_eligible':True,'status':'available','model_version':'v1','horizon_trading_days':20}]))
  self.assertIsNone(prediction_error('券商给出的目标价为100（币种未提取）',[{'kind':'broker_target','target_price':100}]))
  self.assertIsNotNone(prediction_error('模型目标价为100',[{'kind':'broker_target','target_price':100}]))
