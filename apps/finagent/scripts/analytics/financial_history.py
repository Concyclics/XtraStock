"""Revision-aware financial inputs reconstructed from retained vendor versions.

Public reconstruction and actual system observation are separate clocks. Vendor
dates do not certify that the provider has preserved every historical revision.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo

import numpy as np

BEIJING = ZoneInfo('Asia/Shanghai')
CONTRACT = 'vendor-financial-history-v1'
VALUE_NAMES = (
    'reported_gross_margin', 'parent_profit_to_revenue', 'reported_ytd_roe',
    'reported_revenue_growth_yoy', 'reported_parent_profit_growth_yoy',
)
FEATURE_NAMES = (*VALUE_NAMES, 'reported_period_months', 'report_age_days')
MODES = ('public_revision_reconstruction', 'system_observed')


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def local_date(value):
    if not isinstance(value, str):
        raise ValueError('missing_or_invalid_source_date')
    return date.fromisoformat(value[:10])


def aware_timestamp(value):
    result = datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        raise ValueError('system_timestamp_requires_timezone')
    return result.astimezone(timezone.utc)


def normalize_event(event, asset):
    """Preserve reported duration; never label cumulative ROE as annual ROE."""
    raw = event['raw_payload']
    suffix = {'SSE': 'SH', 'SZSE': 'SZ', 'BSE': 'BJ'}.get(asset['exchange'])
    if (event['source'] != 'eastmoney:datacenter' or str(event['asset_id']) != str(asset['asset_id'])
            or raw.get('SECURITY_CODE') != asset['symbol']
            or raw.get('SECUCODE') != f"{asset['symbol']}.{suffix}"):
        raise ValueError('source_security_identity_mismatch')
    period = local_date(str(event['event_date']))
    if local_date(raw.get('REPORTDATE')) != period or (period.month, period.day) not in (
            (3, 31), (6, 30), (9, 30), (12, 31)):
        raise ValueError('invalid_or_mismatched_financial_period')
    notice, updated = local_date(raw.get('NOTICE_DATE')), local_date(raw.get('UPDATE_DATE'))
    if notice <= period:
        raise ValueError('actual_report_not_after_period_end')
    # Date-only announcements and revisions become usable on the following
    # Beijing calendar day, so an after-close release cannot enter that close.
    latest_date = max(notice, updated)
    if raw.get('EITIME'):
        latest_date = max(latest_date, local_date(raw['EITIME']))
    public_at = datetime.combine(latest_date + timedelta(days=1), time(), BEIJING).astimezone(timezone.utc)
    observed_at = max(public_at, aware_timestamp(event['created_at']))
    revenue, profit = number(raw.get('TOTAL_OPERATE_INCOME')), number(raw.get('PARENT_NETPROFIT'))
    percent = lambda key: None if number(raw.get(key)) is None else number(raw[key]) / 100
    values = [percent('XSMLL'), profit/revenue if profit is not None and revenue is not None and revenue > 0 else None,
              percent('WEIGHTAVG_ROE'), percent('YSTZ'), percent('SJLTZ')]
    values = [v if v is None or (math.isfinite(v) and abs(v) <= np.finfo(np.float32).max) else None for v in values]
    if all(v is None for v in values):
        raise ValueError('no_usable_financial_values')
    return {'event_id': int(event['id']), 'asset_id': str(asset['asset_id']), 'period_end': period.isoformat(),
        'period_months': period.month, 'notice_date': notice.isoformat(), 'update_date': updated.isoformat(),
        'public_at': public_at.isoformat(), 'observed_at': observed_at.isoformat(),
        'created_at': aware_timestamp(event['created_at']).isoformat(), 'values': dict(zip(VALUE_NAMES, values)),
        'publication_delay_days': (latest_date-notice).days,
        'cash_per_share_present': number(raw.get('MGJYXJJE')) is not None,
        'book_per_share_present': number(raw.get('BPS')) is not None,
        'contract': CONTRACT, 'production_approved': False}


def distinct_versions(events, mode):
    """Ambiguous same-clock revisions invalidate that period until resolved.

    Later system ingestion is never used to pick a historical public version.
    Identical feature values may share a deterministic source ID for replay.
    """
    if mode not in MODES:
        raise ValueError('unknown_financial_knowledge_mode')
    clock = 'public_at' if mode == MODES[0] else 'observed_at'
    groups = defaultdict(list)
    for event in events:
        groups[(event['asset_id'], event['period_end'], event[clock])].append(event)
    result, conflicts, duplicates = [], 0, 0
    for key, group in groups.items():
        values = {tuple(row['values'][name] for name in VALUE_NAMES) for row in group}
        chosen = dict(min(group, key=lambda row: row['event_id']))
        chosen['known_at'] = key[2]
        chosen['ambiguous'] = len(values) != 1
        chosen['source_event_ids'] = sorted(row['event_id'] for row in group)
        conflicts += int(chosen['ambiguous'])
        duplicates += len(group)-1
        result.append(chosen)
    return sorted(result, key=lambda row: (row['asset_id'], row['known_at'], row['period_end'])), {
        'mode': mode, 'input_events': len(events), 'clock_versions': len(result),
        'ambiguous_versions': conflicts, 'same_clock_extra_rows': duplicates}


def build_feature_panel(events, assets, dates, *, mode, max_age_days=550):
    """Select the latest visible period and revision at each Beijing close.

    Missing values never borrow a different report's denominator or values.
    Most-recent-period conflicts remain missing instead of exposing an older
    convenient report. Freshness is measured from period end, not revision day.
    """
    if not isinstance(max_age_days, int) or max_age_days < 1:
        raise ValueError('invalid_financial_max_age')
    days = [date.fromisoformat(str(d)) for d in dates]
    if days != sorted(set(days)):
        raise ValueError('dates_must_be_unique_and_increasing')
    identities = [str(a['asset_id']) for a in assets]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate_asset_identity')
    allowed = set(identities)
    if any(e['asset_id'] not in allowed for e in events):
        raise ValueError('financial_event_outside_asset_catalog')
    versions, audit = distinct_versions(events, mode)
    by_asset = defaultdict(list)
    for row in versions:
        by_asset[row['asset_id']].append((aware_timestamp(row['known_at']), date.fromisoformat(row['period_end']), row))
    shape = (len(days), len(assets))
    values = np.full((*shape, len(FEATURE_NAMES)), np.nan, dtype=np.float32)
    source_ids = np.full(shape, -1, dtype=np.int64)
    cutoffs = [datetime.combine(d, time(15), BEIJING).astimezone(timezone.utc) for d in days]
    for column, identity in enumerate(identities):
        available, position = {}, 0
        candidates = by_asset[identity]
        for i, (day, cutoff) in enumerate(zip(days, cutoffs)):
            while position < len(candidates) and candidates[position][0] <= cutoff:
                known, period, row = candidates[position]
                available[period] = row
                position += 1
            if not available:
                continue
            period = max(available)
            selected = available[period]
            age = (day-period).days
            if age < 0 or age > max_age_days or selected['ambiguous']:
                continue
            row_values = [np.nan if selected['values'][name] is None else selected['values'][name] for name in VALUE_NAMES]
            values[i, column] = [*row_values, selected['period_months'], age]
            source_ids[i, column] = selected['event_id']
    audit.update(selected_asset_days=int((source_ids >= 0).sum()), max_age_days=max_age_days,
                 decision_clock='15:00 Asia/Shanghai', production_approved=False)
    return values, source_ids, audit
