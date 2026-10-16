"""Reader-facing daily decisions, backed by an immutable plan and research snapshot.

Operational diagnostics stay in the full snapshot. Orders are rendered from the
ledger, never invented by the narrative model or inferred from pool membership.
"""
from __future__ import annotations

import copy
import datetime as dt
from decimal import Decimal, InvalidOperation
import re

TZ = dt.timezone(dt.timedelta(hours=8))
DIAGNOSTIC_SECTIONS = {
    '数据质量与限制', '数据质量与时效', '数据范围与风险', '模型信号边界', '硬门禁阻断项',
}
DIAGNOSTIC_TEXT = re.compile(
    r'本期(?:证据|数据|快照).{0,12}(?:未提供|缺少|不足)|'
    r'(?:未包含|尚未收录).{0,16}(?:收盘|行情)|'
    r'单来源(?:未复权)?(?:行情|价格)|多源核验|正式披露核验|'
    r'(?:尚无|尚未|未能).{0,12}(?:确认.*同业折价|通过.*(?:验证|验收|核验))|'
    r'零收益基线|配对误差|区间覆盖率|置信区间|回测诊断|'
    r'预测误差优势|(?:模型|预测).{0,12}(?:未通过|门槛|质量门禁)|'
    r'来源摘录|(?:系统|数据库|本库).{0,8}(?:缺口|读取失败)|'
    r'不构成投资建议|仅供参考|仅供研究|据此交易.*自负'
)
READER_CONTRACT = """面向读者只写判断、依据、操作条件和跟踪事项。
不生成“数据质量与限制”或系统诊断章节，不复述缺失数据、单来源、回测误差、基线、覆盖率、门禁和免责话术。
这些信息由内部审计保存；没有证据的统计和数值预测直接省略，不写“未提供/暂不提供”的占位解释。
保留有实际决策作用的公司风险和失效条件，例如利润与现金流背离、订单下降、盈利下修。
行情日期在报告头部统一标明；历史财务及预测仍注明真实报告期，不改称最新业绩。
“每日操作清单”由程序按同日账本生成，包括关注、条件买入、参考组合持有/退出和移出关注。
模型负责解释选股逻辑与变化，不另造买卖价格、持仓、成交、目标收益、到期日或重复这张清单。
"""


def reader_claim_error(claim):
    if (claim.get('claim_type') == 'data_quality'
            or claim.get('section_name') in DIAGNOSTIC_SECTIONS
            or DIAGNOSTIC_TEXT.search(str(claim.get('claim_text') or ''))):
        return 'operational_diagnostic_in_reader_claim'
    return None


def model_snapshot(snapshot):
    """An isolated editorial projection; keep approval flags, remove audit prose."""
    result = copy.deepcopy(snapshot)
    for key in ('watermarks', 'required_datasets', 'hard_gate_blockers', 'experiment_registry',
                'research_intelligence', 'investment_advice'):
        result.pop(key, None)
    audit_keys = {'forecast_validation', 'mining', 'quant_framework', 'component_errors',
                  'gaps', 'valuation_gaps', 'limitations', 'blockers', 'validation', 'quality',
                  'factor_coverage', 'paper_portfolio'}

    def project(value):
        if isinstance(value, dict):
            return {k: project(v) for k, v in value.items() if k not in audit_keys}
        if isinstance(value, list):
            return [project(v) for v in value]
        return value

    result = project(result)
    result['evidence'] = [e for e in result.get('evidence', [])
                          if e.get('claim_type') != 'data_quality'
                          and e.get('metric_id') != 'daily_value_plan']
    return result


def load_plan_snapshot(conn, report_date, cutoff, market_date):
    """Read all same-day changes, including removals outside the current top pool."""
    from scripts.analytics.value_plan import digest, load_config
    from scripts.analytics.run_daily_value_plan import publication_approval

    if not conn.execute("SELECT to_regclass('quant.daily_plan_runs') present").fetchone()['present']:
        return {'status': 'not_installed', 'plans': [], 'actionable': False}
    run = conn.execute('''SELECT * FROM quant.daily_plan_runs WHERE report_date=%s
        AND decision_at<=%s AND created_at<=%s ORDER BY decision_at DESC LIMIT 1''',
        (report_date, cutoff, cutoff)).fetchone()
    if not run:
        return {'status': 'missing_current_plan', 'plans': [], 'actionable': False}
    plans = [r['payload'] for r in conn.execute(
        'SELECT payload FROM quant.daily_plan_revisions WHERE run_id=%s ORDER BY stock_code', (run['run_id'],))]
    config = load_config()
    approval_error = None
    try:
        approved, approval = publication_approval(config, cutoff)
    except (ValueError, OSError) as exc:
        approved = False
        approval = {}
        approval_error = type(exc).__name__
    checks = (run.get('validation') or {}).get('checks') or {}
    matching_market = str(run['market_date']) == str(market_date)
    actionable = bool(approved and config.get('publish_enabled') and run['mode'] == 'approved'
                      and run['config_digest'] == digest(config) and checks == approval.get('checks') and checks
                      and all(v is True for v in checks.values()) and matching_market
                      and (not config.get('enabled_from') or str(report_date) >= config['enabled_from']))
    sessions = [str(r['trade_date']) for r in conn.execute('''SELECT trade_date FROM market.trading_calendar
        WHERE exchange='SSE' AND is_verified AND session_status='open'
        AND trade_date BETWEEN %s::date AND %s::date+120 ORDER BY trade_date''', (report_date, report_date))]
    return {'status': 'available', 'run_id': run['run_id'], 'plan_date': str(run['report_date']),
            'market_date': str(run['market_date']), 'decision_at': str(run['decision_at']),
            'mode': run['mode'], 'config_digest': run['config_digest'], 'actionable': actionable,
            'approval_error': approval_error, 'matching_market': matching_market,
            'sessions': sessions, 'plans': plans}


def money(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return f'{number:.2f}' if number.is_finite() and number > 0 else None


def watch_reason(value):
    research = value.get('research') or {}
    facts = [f for f in research.get('actual_financials', [])
             if f.get('quality_status') == 'verified' and f.get('yoy') is not None]
    parts = []
    valuation = research.get('valuation') or {}
    if money(valuation.get('forward_pe')) and valuation.get('forecast_year'):
        parts.append(f"{valuation['forecast_year']}财年预测市盈率{money(valuation['forward_pe'])}倍")
    for fact in sorted(facts, key=lambda f: f.get('metric_code') != 'net_profit_parent')[:2]:
        change = Decimal(str(fact['yoy']))
        if change.is_finite():
            parts.append(f"{fact['period_end']}报告期{fact.get('metric_name') or fact['metric_code']}同比"
                         f"{'增长' if change >= 0 else '下降'}{abs(change):.2f}%")
    if not parts:
        parts = [str(r) for r in research.get('thesis', []) if isinstance(r, str)
                 and not DIAGNOSTIC_TEXT.search(r)][:2]
    return '；'.join(parts)


def build_actions(snapshot, plan_snapshot, watch_limit=6):
    """Keep buy/sell semantics separate from a research inclusion or removal."""
    from scripts.analytics.limit_execution import first_eligible_session

    evidence = snapshot.get('evidence', [])
    current = str(snapshot['report_date'])
    cutoff = snapshot['decision_cutoff']
    actions, suppressed, mentioned = [], [], set()
    enabled = bool(snapshot.get('hard_gate_passed') and plan_snapshot.get('actionable')
                   and plan_snapshot.get('plan_date') == current)
    first = first_eligible_session(cutoff, plan_snapshot.get('sessions', []))
    plan_refs = {e.get('entity_id'): e['evidence_id'] for e in evidence
                 if e.get('metric_id') == 'daily_value_plan'}
    current_plans = (plan_snapshot.get('plans', []) if plan_snapshot.get('plan_date') == current
                     and plan_snapshot.get('matching_market', True) else [])
    for plan in current_plans:
        code, state = plan['stock_code'], plan.get('state')
        quantity = int(plan.get('paper_quantity') or 0)
        action = None
        if state in ('eligible_to_buy', 'pending_limit_entry'):
            if (enabled and plan.get('simulation_eligible') is True and not plan.get('paper_only')
                    and money(plan.get('entry_limit')) and first and plan.get('entry_expires_on')
                    and first <= str(plan['entry_expires_on'])):
                action = 'buy'
            else:
                suppressed.append({'stock_code': code, 'state': state, 'reason': 'entry_not_current_and_approved'})
        elif state in ('reduce', 'exit') and quantity > 0:
            if enabled and money(plan.get('exit_limit')) and plan.get('exit_expires_on') and first and first <= str(plan['exit_expires_on']):
                action = 'sell'
            else:
                action = 'review_holding'
        elif quantity > 0:
            action = 'hold' if state == 'held' else 'review_holding'
        elif state == 'removed':
            action = 'remove'
        if action:
            refs = [plan_refs[code]] if code in plan_refs else []
            if not refs:
                suppressed.append({'stock_code': code, 'reason': 'missing_plan_evidence'})
                continue
            actions.append({'kind': action, 'stock_code': code, 'name': plan.get('name') or code,
                            'evidence_ids': refs, 'plan': plan, 'plan_id': plan.get('plan_id')})
            mentioned.add(code)

    # Watch recommendations use the dated company dossiers. They remain useful
    # even when every trade-plan row is undergoing internal data review.
    watch = [e for e in evidence if e.get('metric_id') == 'frozen_research_watchlist']
    watch.sort(key=lambda e: (not any(f.get('quality_status') == 'verified' for f in
                                     (e.get('value', {}).get('research') or {}).get('actual_financials', [])),
                             (e.get('value') or {}).get('rank') or 999))
    for entry in watch:
        value = entry.get('value') or {}
        code = value.get('stock_code')
        if not code or code in mentioned or sum(a['kind'] == 'watch' for a in actions) >= watch_limit:
            continue
        research = value.get('research') or {}
        focus = watch_reason(value)
        if not focus:
            continue
        actions.append({'kind': 'watch', 'stock_code': code,
                        'name': value.get('name') or research.get('stock_name') or code,
                        'reason': focus, 'evidence_ids': [entry['evidence_id']]})
        mentioned.add(code)
    return {'version': 'integrated-daily-advice-v1', 'plan_run_id': plan_snapshot.get('run_id'),
            'plan_date': plan_snapshot.get('plan_date'), 'plan_status': plan_snapshot.get('status'),
            'mode': plan_snapshot.get('mode'), 'actionable': enabled,
            'actions': actions, 'suppressed': suppressed,
            'published_plan_ids': [a['plan_id'] for a in actions if a['kind'] in ('buy', 'sell')],
            'source_plan': plan_snapshot}


def render_actions(advice):
    groups = (('watch', '今日重点关注'), ('buy', '条件买入'), ('hold', '参考组合持有跟踪'),
              ('review_holding', '参考组合持仓复核'), ('sell', '参考组合减仓与卖出'), ('remove', '移出关注与撤单'))
    lines = []
    for kind, title in groups:
        selected = [a for a in advice.get('actions', []) if a['kind'] == kind]
        if not selected:
            continue
        lines.extend([f'### {title}', ''])
        for action in selected:
            plan = action.get('plan') or {}
            parts = [f"**{action['name']}（{action['stock_code']}）**"]
            if kind == 'watch':
                parts.append(action['reason'])
            elif kind == 'buy':
                parts.append(f"买入限价不高于 {money(plan['entry_limit'])} 元，有效至 {plan['entry_expires_on']}")
            elif kind == 'sell':
                parts.append(f"{'减仓' if plan['state'] == 'reduce' else '卖出'}限价不低于 {money(plan['exit_limit'])} 元，有效至 {plan['exit_expires_on']}")
            elif kind == 'hold':
                parts.append('继续跟踪参考组合持仓及盈利兑现')
            elif kind == 'review_holding':
                parts.append('复核已有参考组合持仓及原投资逻辑')
            else:
                parts.append('移出本期关注名单，取消未成交买入计划')
            if kind in ('buy', 'sell', 'hold'):
                parts.extend(r for r in plan.get('reason', []) if not DIAGNOSTIC_TEXT.search(str(r)))
            if kind in ('buy', 'sell'):
                parts.extend(str(r) for r in (plan.get('invalidation') or [])[:2])
                # Expected returns/dates require an independently validated
                # forecast; relative peer scenarios never become targets here.
                if plan.get('prediction_eligible') is True:
                    if money(plan.get('target_price')):
                        parts.append(f"目标价 {money(plan['target_price'])} 元")
                    if plan.get('expected_net_return') is not None:
                        parts.append(f"预期扣费收益 {Decimal(str(plan['expected_net_return'])) * 100:.2f}%")
                    if plan.get('expected_sell_date'):
                        parts.append(f"预计退出窗口 {plan['expected_sell_date']}")
                if kind == 'sell' and money(plan.get('cost_break_even')):
                    parts.append(f"参考组合成本平衡价 {money(plan['cost_break_even'])} 元")
            refs = ', '.join(action['evidence_ids'])
            lines.append('- ' + '；'.join(parts) + f'。[evidence: {refs}]')
        lines.append('')
    return '\n'.join(lines)
