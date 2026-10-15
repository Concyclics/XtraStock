"""Do not turn a cited spot price/rank into an unapproved numeric prediction."""
from __future__ import annotations
import re

PREDICTIVE_NUMBER=re.compile(r'(?:模型目标(?:价|区间)|目标价|目标区间|预期收益|预计收益|预期回报|上涨概率|触达概率|达成概率)\s*(?:为|是|约|达到|[:：])?\s*[*_]*\s*[-+]?\d|(?:预计|预测)\s*\d+\s*(?:个)?(?:交易日|天|周|月)(?:内|后)?(?:达到|涨至|触达)')

def walk(value):
    if isinstance(value,dict):
        yield value
        for child in value.values(): yield from walk(child)
    elif isinstance(value,list):
        for child in value: yield from walk(child)

def prediction_error(text,evidence):
    if not PREDICTIVE_NUMBER.search(text):return None
    facts=list(walk(evidence))
    if any(row.get('prediction_eligible') is True and row.get('status')=='available'
           and row.get('model_version') and row.get('horizon_trading_days') for row in facts):
        return None
    if re.search(r'券商|机构|研报',text) and '模型目标' not in text and any(
        row.get('kind')=='broker_target' or row.get('explicit_target_price') is not None for row in facts):
        return None
    return 'numeric_prediction_without_approved_scenario'
