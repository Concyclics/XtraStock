"""Separate evidence analysis from investment eligibility in daily reports."""
from __future__ import annotations

import re


BAD_QUALITY = {'rejected', 'quarantined', 'parse_failed', 'template_noise', 'duplicate'}


def usable_evidence(snapshot):
    return [e for e in snapshot.get('evidence', [])
            if e.get('claim_type') != 'data_quality' and e.get('evidence_id')
            and e.get('quality_status') not in BAD_QUALITY]


def analysis_capabilities(snapshot):
    evidence = usable_evidence(snapshot)
    kinds = {e.get('claim_type') for e in evidence}
    return {
        'evidence_analysis': bool(evidence),
        'document_analysis': bool(kinds & {'document_claim', 'actual'}),
        'market_observations': 'market' in kinds,
        'association_analysis': 'graph_association' in kinds,
        'research_focus': any(e.get('metric_id') == 'research_focus' for e in evidence),
        'stock_recommendations': bool(snapshot.get('hard_gate_passed') and (snapshot.get('shared_research') or {}).get('watchlist')),
        'numeric_forecasts': 'only_individually_approved_scenarios',
        'usage': '事实变化与研究关注可独立分析；买卖建议和数值预测另行核验。',
    }


def source_digest(snapshot):
    """Source-attributed extracts on provider failure; never fabricated analysis.

    Every sentence is an exact source title plus an attribution. The ordinary
    claim validator still runs. No blanket override of validation statuses.
    """
    claims = []
    seen = set()
    for e in usable_evidence(snapshot):
        if e.get('claim_type') != 'document_claim':
            continue
        value = e.get('value') or {}
        if not isinstance(value, dict):
            continue
        title = str(value.get('title') or '').strip()
        if not title or title in seen:
            continue
        seen.add(title)
        claims.append({
            'claim_id': f'SRC{len(claims)+1:03d}',
            'section_name': '今日新增新闻与催化', 'claim_type': 'document_claim',
            'claim_text': f'来源摘录：《{title}》。这是已收录的资料标题，内容影响仍需结合正文判断。',
            'evidence_ids': [e['evidence_id']], 'metadata': {
                'recommendation': False, 'generation_mode': 'source_digest'},
        })
        if len(claims) >= 8:
            break
    return claims


def has_actionable_recommendation(claim):
    if (claim.get('metadata') or {}).get('recommendation'):
        return True
    if claim.get('section_name') == '今日投资建议':
        return True
    # Avoid treating a source's rating or a negated recommendation as an order.
    text = str(claim.get('claim_text') or '')
    text = re.sub(r'(?:不|勿|暂不|不宜|不能|不应)(?:建议|推荐)?(?:买入|卖出|加仓|减仓|增持|建仓)', '', text)
    directed = r'(?:建议|推荐|应当|可以|优先|考虑)(?:重点|立即|现在|逢低|逢高|适度|适当)?(?:买入|卖出|加仓|减仓|建仓|配置|增持|减持|重仓)'
    imperative = r'(?:^|[。；！!\n])\s*(?:立即|现在|逢低|逢高)?(?:买入|卖出|加仓|减仓|建仓|重仓|满仓)'
    return bool(re.search(directed, text) or re.search(imperative, text))


def claim_evidence_error(claim, selected):
    if any(e.get('quality_status') in BAD_QUALITY for e in selected):
        return 'rejected_or_quarantined_source'
    # A heuristic focus priority/count is not a company financial metric.
    only_focus=bool(selected) and all(e.get('metric_id')=='research_focus' for e in selected)
    if only_focus and re.search(r'营收|营业收入|净利润|净利率|现金流|利润率|每股收益', str(claim.get('claim_text') or '')) and re.search(r'\d',str(claim.get('claim_text') or '')):
        if not any((e.get('value') or {}).get('context',{}).get('financials') for e in selected):
            return 'financial_number_without_financial_evidence'
    return None


def evidence_numeric_projection(evidence):
    if evidence.get('metric_id')!='research_focus':
        return evidence
    value=dict(evidence.get('value') or {})
    # Do not let policy scores or review timers authorize financial numbers.
    for key in ('priority','next_review_at','entered_active_at','first_seen_at','low_signal_days'):
        value.pop(key,None)
    return {**evidence,'value':value}
