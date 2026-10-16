"""Reconstruct daily P&L from frozen fills and marks, without making orders.

This is an accounting decomposition, not causal factor/sector attribution.
Corporate-action parsing uses the same documented input contract as the ledger;
cash, shares, daily marks and contributions are independently accumulated here.
"""
from __future__ import annotations

from collections import defaultdict
import math

import numpy as np

from execution_diagnostics import audit_execution
from strategy_research import action_schedule

COMPONENTS = ('holding_market_pnl', 'action_mark_pnl', 'net_dividend',
              'trade_to_close_pnl', 'fees', 'liquidation_provision', 'net_pnl')


def attribute_daily(result, data):
    audit_execution(result)
    dates = [str(d) for d in data['dates']]
    symbols = [str(s) for s in data['symbols']]
    if len(set(dates)) != len(dates) or dates != sorted(dates) or len(set(symbols)) != len(symbols):
        raise ValueError('Ambiguous source calendar or security identity')
    day_index = {d: i for i, d in enumerate(dates)}
    symbol_index = {s: i for i, s in enumerate(symbols)}
    selected = [day_index[d] for d in result['dates']]
    if not selected or selected[0] < 1 or selected != list(range(selected[0], selected[-1]+1)):
        raise ValueError('Account calendar is not contiguous within market panel')
    if len(result['nav']) != len(selected)+1 or len(result['exposure']) != len(selected):
        raise ValueError('NAV requires an initial value plus one mark per session')
    if data.get('primary_since') and result['dates'][0] < data['primary_since']:
        raise ValueError('Feature warm-up cannot enter attribution returns')
    policy = result['policy']; capital = float(policy['initial_cash'])
    tolerance = max(1e-5, capital*1e-9)
    if result['nav'][0] != capital:
        raise ValueError('Initial NAV differs from capital')
    fills = defaultdict(list); logged_actions = defaultdict(list)
    for row in result['execution_trace']['fills']:
        if row['date'] not in result['dates'] or row['symbol'] not in symbol_index:
            raise ValueError('Fill lies outside account or market scope')
        fills[row['date']].append(row)
    for row in result['execution_trace']['corporate_actions']:
        logged_actions[row['date']].append(row)
    if any(day not in result['dates'] for day in logged_actions):
        raise ValueError('Action lies outside account calendar')
    decisions = {row['date']: row for row in result.get('rebalance_log', [])}
    if len(decisions) != len(result.get('rebalance_log', [])):
        raise ValueError('Duplicate decision day')
    actions, _ = action_schedule(data)
    shares = np.zeros(len(symbols)); cash = capital; receivable = 0.
    marks = np.nan_to_num(data['close'][selected[0]-1], nan=0.)
    payments = defaultdict(float); daily = []; assets = []
    max_nav_error = max_exposure_error = max_cash_error = 0.
    previous_target = 0.
    for offset, t in enumerate(selected):
        day = dates[t]; old_shares = shares.copy(); old_marks = marks.copy()
        reference = marks.copy(); dividend = np.zeros(len(symbols)); expected_actions = []
        for j, extra, gross, pay, quality in actions.get(t, []):
            reference[j] = (reference[j]-gross)/(1+extra) if reference[j] > 0 else 0.
            if shares[j] <= 1e-7:
                continue
            net = shares[j]*gross*(1-policy['dividend_tax'])
            expected_actions.append({'date': day, 'symbol': symbols[j], 'shares_before': float(shares[j]),
                'extra_shares_per_share': extra, 'net_dividend': float(net), 'payment_index': pay, 'quality': quality})
            shares[j] *= 1+extra
            dividend[j] += net; receivable += net; payments[pay] += net
        if expected_actions != logged_actions.get(day, []):
            raise ValueError('Source actions differ from held entitlements: '+day)
        paid = payments.pop(t, 0.); cash += paid; receivable -= paid
        action_mark = shares*reference-old_shares*old_marks
        before_trades = shares.copy(); trade_cash = np.zeros(len(symbols)); fees = np.zeros(len(symbols))
        for fill in fills.get(day, []):
            j = symbol_index[fill['symbol']]; sign = 1 if fill['side'] == 'buy' else -1
            shares[j] += sign*fill['shares']
            trade_cash[j] += sign*fill['notional']; fees[j] += fill['fee']
            cash -= sign*fill['notional']+fill['fee']
        o, h, l, c = (data[k][t] for k in ('open', 'high', 'low', 'close'))
        valid = ((data['quality'][t] > 0) & (data['quality'][t] < 3) & np.isfinite(o+h+l+c)
                 & (l > 0) & (h >= np.maximum(o, c)) & (l <= np.minimum(o, c)))
        marks = np.where(valid, c, reference)
        held_pnl = before_trades*(marks-reference)
        trade_pnl = (shares-before_trades)*marks-trade_cash
        provision = shares*marks*policy['cost_bps']/10000 if offset == len(selected)-1 else np.zeros(len(symbols))
        pnl = held_pnl+action_mark+dividend+trade_pnl-fees-provision
        before_provision = cash+float(shares@marks)+receivable
        nav = before_provision-float(provision.sum())
        nav_error = nav-result['nav'][offset+1]
        pnl_error = float(pnl.sum())-(result['nav'][offset+1]-result['nav'][offset])
        exposure = float(shares@marks)/before_provision
        exposure_error = exposure-result['exposure'][offset]
        max_nav_error = max(max_nav_error, abs(nav_error), abs(pnl_error))
        max_exposure_error = max(max_exposure_error, abs(exposure_error))
        if max_nav_error > tolerance or max_exposure_error > 1e-10 or cash < -tolerance or np.any(shares < -1e-6):
            raise ValueError('Daily cash/share/NAV/exposure reconciliation failed: '+day)
        if day in decisions:
            step = decisions[day]
            max_cash_error = max(max_cash_error, abs(cash-step['cash']), abs(before_provision-step['nav']))
            if max_cash_error > tolerance:
                raise ValueError('Recorded decision cash or NAV differs: '+day)
            previous_target = step['target_allocation']
        values = dict(zip(COMPONENTS, (held_pnl, action_mark, dividend, trade_pnl, fees, provision, pnl)))
        daily.append({'date': day, 'nav_before': result['nav'][offset], 'nav_after': nav, 'cash': cash,
            'receivable': receivable, 'dividends_paid': paid, 'exposure': exposure, 'target_allocation': previous_target,
            'positions': int((shares > 1e-7).sum()), 'nav_error': nav_error, 'pnl_error': pnl_error,
            **{key: float(value.sum()) for key, value in values.items()}})
        active = (old_shares > 1e-7) | (shares > 1e-7) | (fees > 0) | (dividend != 0)
        assets.extend({'date': day, 'symbol': symbols[j], 'shares_before': float(old_shares[j]),
            'shares_after': float(shares[j]), 'marked_value': float(shares[j]*marks[j]),
            **{key: float(value[j]) for key, value in values.items()}} for j in np.flatnonzero(active))
    terminal = {r['symbol']: r for r in result['execution_trace']['terminal_positions']}
    for j, symbol in enumerate(symbols):
        if abs(shares[j]-terminal.get(symbol, {}).get('shares', 0.)) > 1e-6:
            raise ValueError('Final shares differ: '+symbol)
        if symbol in terminal and not math.isclose(marks[j], terminal[symbol]['mark'], rel_tol=1e-12, abs_tol=1e-9):
            raise ValueError('Final mark differs: '+symbol)
    if abs(receivable-result['metrics']['terminal_receivables']) > tolerance:
        raise ValueError('Terminal dividend receivable differs')
    return {'version': 'daily-nav-attribution-v1', 'status': 'passed', 'sessions': len(selected),
        'maximum_nav_or_pnl_error_cny': max_nav_error, 'maximum_exposure_error': max_exposure_error,
        'maximum_checkpoint_error_cny': max_cash_error, 'tolerance_cny': tolerance,
        'daily': daily, 'asset_days': assets, 'production_approved': False,
        'scope': 'Accounting on frozen simulated fills, including stale marks. Not causal factor attribution or proof of executable prices.'}


def summarize_window(attribution, start, end):
    """Inclusive session window. Drawdown starts the session AFTER its peak."""
    selected = [r for r in attribution['daily'] if start <= r['date'] <= end]
    if not selected or selected[0]['date'] != start or selected[-1]['date'] != end:
        raise ValueError('Window endpoints must be observed account sessions')
    by_symbol = defaultdict(lambda: {key: 0. for key in COMPONENTS})
    for row in attribution['asset_days']:
        if start <= row['date'] <= end:
            for key in COMPONENTS:
                by_symbol[row['symbol']][key] += row[key]
    totals = {key: math.fsum(row[key] for row in selected) for key in COMPONENTS}
    initial = selected[0]['nav_before']; final = selected[-1]['nav_after']
    if abs(totals['net_pnl']-(final-initial)) > attribution['tolerance_cny']:
        raise ValueError('Window P&L does not reconcile')
    assets = sorted([{'symbol': symbol, **row, 'contribution_start_nav': row['net_pnl']/initial}
                     for symbol, row in by_symbol.items()], key=lambda row: (row['net_pnl'], row['symbol']))
    return {'start': start, 'end': end, 'sessions': len(selected), 'nav_start': initial, 'nav_end': final,
        'net_return': final/initial-1, **totals,
        'average_exposure': float(np.mean([r['exposure'] for r in selected])),
        'minimum_exposure': min(r['exposure'] for r in selected), 'maximum_exposure': max(r['exposure'] for r in selected),
        'reduced_target_days': sum(r['target_allocation'] < 1-1e-12 for r in selected),
        'securities': len(assets), 'losing_securities': sum(r['net_pnl'] < 0 for r in assets),
        'top10_loss_contribution_start_nav': sum(r['contribution_start_nav'] for r in assets[:10] if r['net_pnl'] < 0),
        'asset_contributions': assets}
