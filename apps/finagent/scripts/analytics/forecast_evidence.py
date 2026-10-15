"""Small, typed forecast-validation evidence shared by daily reports and chat."""
from __future__ import annotations
import datetime as dt
from zoneinfo import ZoneInfo

CHECK_LABELS = {
    'latest_beats_simple_baselines': '最近测试误差仍未优于全部简单基线',
    'coverage_80_between_75_and_90': '名义80%区间的实际覆盖率不在75%至90%范围',
    'three_complete_outer_folds': '完整滚动测试不足三折',
    'at_least_two_fold_wins': '三折回测中胜过简单基线的次数不足两次',
    'paired_improvement_confident': '按日期分组后，误差改善的95%区间仍包含零',
    'interval_score_not_worse': '同时考虑宽度与漏覆盖的区间评分没有优于基线',
    'five_calibration_holding_windows': '校准样本不足五个持有期长度',
}
BASELINE_LABELS = {'zero': '零收益', 'training_median': '训练期收益中位数',
                   'momentum': '20日动量外推', 'reversal': '20日反转'}


def validation_evidence(model):
    evaluation = model['evaluation']; test = evaluation['final_test']
    interval = test.get('paired_mae_difference') or {}
    folds = evaluation.get('outer_folds') or []
    spec = evaluation.get('selected_spec') or {}
    return {
        'horizon_trading_days': model['horizon'], 'market_date': str(model['as_of']),
        'updated_at': dt.datetime.fromisoformat(str(model['created_at']).replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).isoformat(),
        'research_validation_passed': bool(evaluation['research_validation_passed']),
        'prediction_eligible': False, 'production_approved': False,
        'test_period': {'signal_from': test['start'], 'signal_through': test['end'],
            'outcomes_through': test.get('outcomes_through'), 'rows': test['rows'], 'signal_dates': test['dates']},
        'average_error_percentage_points': round(test['mae'] * 100,3),
        'baseline_errors_percentage_points': {BASELINE_LABELS.get(k,k): round(v*100,3)
            for k,v in (test.get('baseline_maes') or {'zero':test['zero_return_mae'],'momentum':test['momentum_mae']}).items()},
        'observed_interval_coverage_percent': round(test['coverage_80'] * 100,2),
        'nominal_interval_coverage_percent': 80,
        'paired_error_difference_percentage_points': {k:round(interval[k]*100,3) for k in ('mean','lower_95','upper_95') if k in interval},
        'comparison_baseline': BASELINE_LABELS.get(test.get('selected_baseline'), '零收益与动量'),
        'paired_difference_weighting': '先按每个信号日平均，再对日期等权；上方平均误差按股票日期样本等权。配对差值不等于上方两个均值直接相减。',
        'rolling_fold_count': len(folds), 'rolling_fold_wins': evaluation.get('outer_fold_wins'),
        'calibration_signal_dates': evaluation.get('splits',{}).get('calibration',{}).get('dates'),
        'training_design': {'estimator': {'ridge_raw':'正则化线性回归','huber_risk_normalized':'按波动率归一的稳健回归'}.get(spec.get('family'),'线性回归'),
            'training_window_sessions': spec.get('window') or '全部可用训练日期',
            'inputs': ['5日涨跌幅','20日涨跌幅','20日波动率','20日回撤','20日量比'],
            'derived_inputs': '稳健模型另含风险归一趋势、短长趋势差、对数量比交互；只按训练段缩放',
            'selection': '仅用调参段误差选模型；校准段和测试段不参与选参',
            'excluded': '财务、公告、图谱尚缺足够长且按历史时点可得的记录，未回填成历史训练特征'},
        'failed_requirements': [CHECK_LABELS.get(k, '评估要求尚未满足') for k in evaluation.get('failed_checks',[])],
        'fold_comparisons': [{'signal_from':f['final_test']['start'],'signal_through':f['final_test']['end'],
            'average_error_percentage_points':round(f['final_test']['mae']*100,3),
            'zero_baseline_error_percentage_points':round(f['final_test']['zero_return_mae']*100,3),
            'observed_coverage_percent':round(f['final_test']['coverage_80']*100,2),
            'beat_all_simple_baselines':f['final_test']['beats_all_baselines']} for f in folds],
        'evidence_scope': '当前20只以内样本的历史重构；测试区间曾用于研究，不是新独立验证，也不是实盘收益。',
        'interpretation': '误差单位为百分点；覆盖率不是上涨概率。若按日期分组的差值区间跨零，则尚未证明误差优势。',
        'display_precision': '误差及差值区间保留3位小数，覆盖率保留2位；门槛用本机未舍入指标判断。',
        'forecast_use': ('历史研究通过，更新模型仍等待真实前瞻结果；不自动生成交易目标' if evaluation['research_validation_passed'] else
            '用已验证行情、财务、公告和业务关联说明趋势驱动与后续验证条件；不把排序分或回测收益换算成目标价。'),
    }
