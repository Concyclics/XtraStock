"""Ex-post evidence for a simulated cashbook, never an order-sizing input.

Daily share volume, observed turnover, and a turnover lower bound answer
different questions. None proves that a next-open auction fill was available.
"""
from __future__ import annotations

from collections import defaultdict
import math


def positive(value):
    return value is not None and math.isfinite(value) and value > 0


def classify_fill(fill, participation):
    if not 0 < participation <= .05:
        raise ValueError('Invalid participation limit')
    volume, amount = fill['daily_volume_lots'], fill['daily_amount_observed']
    low, high = fill['daily_low'], fill['daily_high']
    known_volume = positive(volume)
    observed = positive(amount)
    lower = low * volume * 100 if known_volume and positive(low) else None
    usable_lower = positive(lower)
    capacity_amount = amount if observed else lower if usable_lower else 0.
    volume_breach = bool(known_volume and fill['shares'] > volume * 100 * participation + 1e-7)
    notional_breach = bool(fill['notional'] > capacity_amount * participation + 1e-7)
    kind = 'observed' if observed else 'lower_bound' if usable_lower else 'unavailable'
    bounds_known = bool(known_volume and positive(low) and positive(high) and high >= low)
    return {
        'amount_kind': kind,
        'daily_share_participation': fill['shares'] / (volume * 100) if known_volume else None,
        'share_volume_breach': volume_breach,
        'unknown_daily_volume': not known_volume,
        'legacy_notional_breach': notional_breach,
        'observed_notional_breach': notional_breach and observed,
        'lower_bound_breach': notional_breach and kind == 'lower_bound',
        'lower_bound_only_breach': notional_breach and kind == 'lower_bound' and known_volume and not volume_breach,
        'unavailable_amount_breach': notional_breach and kind == 'unavailable',
        'daily_price_range_unknown': not (positive(low) and positive(high) and high >= low),
        'price_outside_daily_range': bool(positive(low) and positive(high) and
            (fill['price'] < low - 1e-7 or fill['price'] > high + 1e-7)),
        # A 1% tolerance accommodates provider rounding, not a unit conversion.
        'observed_amount_volume_inconsistent': bool(observed and bounds_known and
            (amount < low * volume * 100 * .99 or amount > high * volume * 100 * 1.01)),
        'daily_quality_excluded': fill['daily_quality'] <= 0 or fill['daily_quality'] >= 3,
    }


def audit_execution(result):
    """Independently reconcile fills, shares, fees and marked asset P&L.

    Attribution includes still-open/stale positions at the cashbook's marks;
    it is accounting evidence, not a claim that those values are realizable.
    """
    trace = result['execution_trace']
    if trace['version'] != 'execution-trace-v1' or trace['volume_unit'] != 'lots_of_100_shares':
        raise ValueError('Unknown execution trace or volume unit')
    policy = result['policy']; participation = policy['participation']
    capital = policy['initial_cash']; tolerance = max(1e-5, capital * 1e-9)
    fills = trace['fills']; counts = defaultdict(int); years = defaultdict(lambda: defaultdict(int))
    assets = defaultdict(lambda: {'buy_notional': 0., 'sell_notional': 0., 'fees': 0., 'net_dividends': 0.,
        'fill_count': 0, 'share_volume_breaches': 0, 'legacy_notional_breaches': 0})
    bad_fills = []; day_fills = defaultdict(list); day_actions = defaultdict(list)
    fill_keys = set()
    for fill in fills:
        key = (fill['date'], fill['symbol'], fill['side'])
        if key in fill_keys or fill['side'] not in ('buy', 'sell'):
            raise ValueError('Duplicate or invalid simulated fill')
        fill_keys.add(key)
        if not fill['signal_date'] < fill['date'] or not positive(fill['shares']) or not positive(fill['price']):
            raise ValueError('Noncausal or invalid fill')
        if not math.isclose(fill['notional'], fill['shares'] * fill['price'], rel_tol=1e-12, abs_tol=1e-7):
            raise ValueError('Fill notional does not reconcile')
        if not math.isclose(fill['fee'], fill['notional'] * policy['cost_bps'] / 10000, rel_tol=1e-12, abs_tol=1e-7):
            raise ValueError('Fill fee does not reconcile')
        label = classify_fill(fill, participation)
        counts['fill_count'] += 1
        counts['amount_' + label['amount_kind']] += 1
        years[fill['date'][:4]]['fill_count'] += 1
        for name, value in label.items():
            if isinstance(value, bool):
                counts[name] += int(value); years[fill['date'][:4]][name] += int(value)
        asset = assets[fill['symbol']]
        asset[fill['side'] + '_notional'] += fill['notional']; asset['fees'] += fill['fee']
        asset['fill_count'] += 1; asset['share_volume_breaches'] += int(label['share_volume_breach'])
        asset['legacy_notional_breaches'] += int(label['legacy_notional_breach'])
        if any(value for name, value in label.items() if isinstance(value, bool)):
            bad_fills.append({**fill, **label})
        day_fills[fill['date']].append(fill)
    for action in trace['corporate_actions']:
        day_actions[action['date']].append(action)
        assets[action['symbol']]['net_dividends'] += action['net_dividend']
    quantities = defaultdict(float)
    for day in sorted(set(day_fills) | set(day_actions)):
        for action in day_actions[day]:
            symbol = action['symbol']
            if not math.isclose(quantities[symbol], action['shares_before'], rel_tol=1e-10, abs_tol=1e-6):
                raise ValueError('Corporate-action entitlement does not reconcile')
            quantities[symbol] *= 1 + action['extra_shares_per_share']
        grouped = defaultdict(float)
        for fill in day_fills[day]:
            sign = 1 if fill['side'] == 'buy' else -1
            quantities[fill['symbol']] += sign * fill['shares']
            if quantities[fill['symbol']] < -1e-6:
                raise ValueError('Fill trace sells unowned shares')
            grouped[fill['symbol']] += fill['notional']
            if grouped[fill['symbol']] > fill['prior_liquidity_cap_notional'] + tolerance:
                raise ValueError('Fill exceeds its declared prior-session liquidity cap')
    terminal = {row['symbol']: row for row in trace['terminal_positions']}
    if len(terminal) != len(trace['terminal_positions']):
        raise ValueError('Duplicate terminal position')
    attribution = []
    for symbol in sorted(set(assets) | set(quantities) | set(terminal)):
        final = terminal.get(symbol, {'shares': 0., 'marked_value': 0., 'liquidation_fee_provision': 0., 'stale_sessions': 0})
        if not math.isclose(quantities[symbol], final['shares'], rel_tol=1e-10, abs_tol=1e-6):
            raise ValueError('Terminal shares do not reconcile')
        asset = assets[symbol]
        pnl = (asset['sell_notional'] - asset['buy_notional'] - asset['fees'] + asset['net_dividends']
               + final['marked_value'] - final['liquidation_fee_provision'])
        attribution.append({'symbol': symbol, **asset, 'terminal_marked_value': final['marked_value'],
            'terminal_stale_sessions': final['stale_sessions'], 'terminal_fee_provision': final['liquidation_fee_provision'],
            'net_pnl': pnl, 'contribution_initial_capital': pnl / capital})
    profit = math.fsum(row['net_pnl'] for row in attribution)
    fees = math.fsum(row['fees'] for row in attribution)
    provision = math.fsum(row['terminal_fee_provision'] for row in attribution)
    for actual, expected, label in ((profit, result['nav'][-1] - capital, 'NAV profit'),
        (fees, result['metrics']['fees_paid'], 'fees'),
        (provision, result['metrics']['liquidation_fee_provision'], 'terminal fee provision')):
        if abs(actual - expected) > tolerance:
            raise ValueError(label + ' does not reconcile')
    if counts['fill_count'] != result['metrics']['fill_count']:
        raise ValueError('Fill count does not reconcile')
    legacy = result['metrics']['blocked']
    if counts['legacy_notional_breach'] != legacy['ex_post_capacity_breach']:
        raise ValueError('Legacy notional diagnostic does not reconcile')
    if counts['unknown_daily_volume'] != legacy['ex_post_no_volume_fill']:
        raise ValueError('Missing-volume diagnostic does not reconcile')
    return {'version': 'execution-audit-v1', 'counts': dict(counts), 'by_year': dict(years),
        'asset_attribution': attribution, 'flagged_fills': bad_fills,
        'accounting': {'status': 'passed', 'net_pnl': profit, 'fees': fees, 'terminal_fee_provision': provision,
            'nav_reconciliation_error': profit - (result['nav'][-1] - capital), 'tolerance_cny': tolerance},
        'execution_verified': False, 'production_approved': False,
        'limitation': 'Daily volume compatibility does not certify next-open auction capacity or execution. Attribution retains stale marks and is not realizable liquidation profit.'}
