"""Prepare bounded, secret-free JSON payloads for remote daily-report models."""
from __future__ import annotations

import json
from typing import Any


LEGACY_LIST_LIMITS = {
    "same_day_news": 15,
    "same_day_news_keywords": 20,
    "same_day_events": 15,
    "top_events_7d": 15,
    "theme_summary": 24,
    "top_theme_stocks": 24,
    "same_day_ai_hardware_reports": 20,
    "recent_ai_hardware_reports": 20,
    "fusion_top": 18,
    "factor_catalog": 18,
    "factor_extremes": 24,
    "temporal_graph_evidence": 24,
    "temporal_graph_coverage": 24,
    "prior_daily_reports": 3,
}
V3_METRIC_LIMITS = {
    "market_coverage": 1,
    "valuation_screen": 12,
    "sector_research_attention": 12,
    "candidate_pool": 24,
    "frozen_research_watchlist": 20,
    "daily_value_plan": 4,
    "dataset_watermark": 20,
    "published_document": 30,
    "broker_report": 30,
    "total_return_close": 20,
    "raw_close": 30,
}
V3_DEFAULT_METRIC_LIMIT = 12
SECRET_KEY_MARKERS = ("password", "secret", "api_key", "token", "credential")


def sanitized(value: Any, *, max_string_chars: int = 1800) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitized(item, max_string_chars=max_string_chars)
            for key, item in value.items()
            if not any(marker in str(key).lower() for marker in SECRET_KEY_MARKERS)
        }
    if isinstance(value, list):
        return [sanitized(item, max_string_chars=max_string_chars) for item in value]
    if isinstance(value, str) and len(value) > max_string_chars:
        return value[:max_string_chars] + "...[truncated]"
    return value


def compact_legacy_data_pack(data_pack: dict[str, Any]) -> dict[str, Any]:
    compact = sanitized(data_pack, max_string_chars=1200)
    for key, limit in LEGACY_LIST_LIMITS.items():
        value = compact.get(key)
        if isinstance(value, list) and len(value) > limit:
            compact[key] = value[:limit]
    # The shared pool is mandatory. Trim the largest optional legacy list
    # before calling the provider, retaining at least one item of every type.
    while len(json.dumps(compact,ensure_ascii=False,default=str))>185000:
        candidates=[key for key in LEGACY_LIST_LIMITS if isinstance(compact.get(key),list) and len(compact[key])>1]
        if not candidates: raise ValueError('Required legacy evidence exceeds model input budget')
        key=max(candidates,key=lambda k:len(json.dumps(compact[k],ensure_ascii=False,default=str)))
        compact[key].pop()
    compact["llm_payload_metadata"] = {
        "source": "data_pack.json",
        "list_limits": LEGACY_LIST_LIMITS,
        "strings_truncated_at_chars": 1200,
        "numeric_values_unchanged": True,
    }
    return compact


def compact_v3_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep representative evidence from every metric within remote API limits."""
    compact = sanitized(snapshot, max_string_chars=1200)
    # A registry catalog describes available experiments; it is not an evidence
    # row containing their results. Hundreds of unused metric definitions used
    # to consume the mandatory budget even after every optional row was pruned.
    # Preserve methodology and qualification; units on actual evidence stay intact.
    registry=compact.get('experiment_registry')
    if isinstance(registry,dict):
        referenced_projects={item.get('value',{}).get('project') for item in compact.get('evidence',[])
            if isinstance(item.get('value'),dict)}
        for project in registry.get('projects',[]):
            contract=project.get('result_contract')
            if isinstance(contract,dict):
                project['result_contract']={k:v for k,v in contract.items() if k in
                    ('status','prediction_eligible','gate_status','methodology_notes','year_notes','comparison_dimensions')}
                if project.get('project_key') not in referenced_projects:
                    project['result_contract']={k:v for k,v in project['result_contract'].items()
                        if k in ('status','prediction_eligible','gate_status')}
                    project['results_in_payload']=False
    if "shared_research" in compact:
        compact["shared_research"]={k:v for k,v in compact["shared_research"].items() if k not in ('rows','watchlist')}
    evidence = compact.get("evidence")
    if not isinstance(evidence, list):
        return compact

    selected: list[dict[str, Any]] = []
    metric_counts: dict[str, int] = {}
    for item in evidence:
        metric_id = str(item.get("metric_id") or "unknown")
        limit = V3_METRIC_LIMITS.get(metric_id, V3_DEFAULT_METRIC_LIMIT)
        count = metric_counts.get(metric_id, 0)
        if count >= limit:
            continue
        selected.append(item)
        metric_counts[metric_id] = count + 1

    # Preserve all watchlist decisions; remove duplicated context and bound
    # ancillary detail instead of dropping a stock or an evidence identifier.
    for item in selected:
        value=item.get('value') or {}
        if not isinstance(value,dict):
            continue  # Scalar factor evidence is already bounded and must survive unchanged.
        if value.get('component')=='candidate_pool_summary':
            mining=value.get('mining') or {};notes=[]
            for horizon in mining.get('horizons',[]):
                for fold in horizon.get('folds',[]):
                    # The current estimator supersedes this legacy value. Keep
                    # every fold's current statistics, cost results and verdict.
                    fold.pop('legacy_test_rank_ic',None)
                    purpose=fold.pop('purpose',None)
                    backtest=fold.get('cost_backtest') or {}
                    limitation=backtest.pop('limitations',None)
                    for note in (purpose,limitation):
                        if note and note not in notes:notes.append(note)
            if notes:mining['shared_fold_notes']=notes
        if item.get('metric_id')!='frozen_research_watchlist':
            continue
        research=value.get('research') or {}
        reason=value.get('reason')
        if isinstance(reason,dict):
            # These detailed factor records duplicate the research projection.
            # Retain the actual decision reasons, caveats and invalidation.
            reason.pop('production_factors',None);reason.pop('factor_coverage',None)
            for explanation in reason.get('reasons',[]):
                if isinstance(explanation.get('evidence'),list):explanation['evidence']=explanation['evidence'][:1]
                if isinstance(explanation.get('factors'),list):explanation['factors']=explanation['factors'][:2]
        # The outer value already binds membership, reason and rank. Keeping a
        # second complete decision crowds dynamic candidates out of the prompt.
        research.pop('watchlist_membership',None)
        for key in ('graph_changes',): research.pop(key,None)
        research['actual_financials']=[{k:v for k,v in fact.items() if k!='asset_id'}
            for fact in research.get('actual_financials',[])[:2]]
        research['factors']=[{k:v for k,v in factor.items() if k!='version'} for factor in research.get('factors',[])[:3]]
        research['graph']=[{k:v for k,v in row.items() if k in (
            'source_name','target_name','relation_type','valid_from','valid_to','public_known_from',
            'review_status','source_system','document_id','evidence_text','claim_class')}
            for row in research.get('graph',[])[:1]]
        research['broker_targets']=[{k:v for k,v in row.items() if k in (
            'institution','report_date','target_price','currency','horizon_months','valid_until',
            'expired','price_basis','kind','source_url','document_id')}
            for row in research.get('broker_targets',[])[:1]]
        for scenario in research.get('scenarios',[]):
            scenario.pop('validation',None)  # Detailed OOS statistics stay in the saved snapshot.
            scenario.pop('model_id',None)  # Internal immutable identifier is not reader evidence.
            if scenario.get('prediction_eligible') is not True:
                # Preserve the explicit failure and its reasons. Empty price/
                # probability fields and repeated pricing assumptions cannot
                # support a prediction and used to crowd out current validation.
                keep={'status','blockers','prediction_eligible','horizon_trading_days','model_version'}
                for key in list(scenario):
                    if key not in keep:scenario.pop(key)
    compact.pop('watermarks',None)  # DQ evidence already includes the frozen watermarks.
    pool_members=[item for item in selected if isinstance(item.get('value'),dict)
                  and item['value'].get('component')=='candidate_pool']
    protected_pool_ids={item['evidence_id'] for item in pool_members[:2]}
    protected_pool_ids.update(item['evidence_id'] for item in
        [row for row in selected if row.get('metric_id')=='daily_value_plan'][:2])
    protected_pool_ids.update(item['evidence_id'] for item in selected
        if isinstance(item.get('value'),dict) and item['value'].get('component')=='candidate_pool_summary')
    # The report's three main inputs remain present under size pressure.
    # Preserve two valuation companies and two industry attention rows, as well
    # as source-diverse news and the explicit universe coverage statement.
    for metric in ('valuation_screen','sector_research_attention','market_coverage'):
        protected_pool_ids.update(item['evidence_id'] for item in
            [row for row in selected if row.get('metric_id')==metric][:2])
    sources=set()
    for item in selected:
        if item.get('metric_id')=='published_document':
            source=(item.get('value') or {}).get('source_name')
            if source not in sources:
                sources.add(source);protected_pool_ids.add(item['evidence_id'])
    # Include the metadata overhead in the budget, not just the evidence array.
    compact["llm_payload_metadata"] = {
        "source": "data_snapshot.json", "original_evidence_count": len(evidence),
        "included_evidence_count": len(selected), "metric_limits": V3_METRIC_LIMITS,
        "default_metric_limit": V3_DEFAULT_METRIC_LIMIT,
        "strings_truncated_at_chars": 1200, "numeric_values_unchanged": True,
    }
    # Exact whole-entry pruning guarantees a bounded payload as pools grow.
    # Keep at least one item for each evidence prefix and every POOL item.
    while len(json.dumps({**compact,'evidence':selected},ensure_ascii=False,default=str))>90000:
        counts={}
        for item in selected:
            key=str(item.get('evidence_id','')).rstrip('0123456789')
            counts[key]=counts.get(key,0)+1
        removable=[(i,item) for i,item in enumerate(selected)
            if item.get('metric_id')!='frozen_research_watchlist'
            and item.get('evidence_id') not in protected_pool_ids
            and counts[str(item.get('evidence_id','')).rstrip('0123456789')]>1]
        if not removable:
            raise ValueError('Required research evidence exceeds the safe model input budget')
        largest=max(removable,key=lambda pair:(counts[str(pair[1].get('evidence_id','')).rstrip('0123456789')],pair[0]))
        selected.pop(largest[0])
    compact["evidence"] = selected
    compact["llm_payload_metadata"]["included_evidence_count"] = len(selected)
    return compact


def inline_json(payload: dict[str, Any], *, max_chars: int = 200_000) -> str:
    text = json.dumps(sanitized(payload), ensure_ascii=False, separators=(",", ":"), default=str)
    if len(text) > max_chars:
        raise ValueError(f"inline model payload exceeds {max_chars} characters: {len(text)}")
    return text
