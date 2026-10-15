"""Freeze score provenance with the data actually used by a research snapshot."""
import hashlib
import json


def freeze_contract(feature, scores):
    if not feature:
        return {'version':'snapshot-input-v1','strict_pit_ready':False,'reason':'missing_current_feature'}
    strict=(feature.get('input_contract_version')=='pit-input-v1' and feature.get('knowledge_mode')=='system'
            and feature.get('input_quality',{}).get('strict_pit_ready') is True)
    valid_names=[]
    for score in scores:
        if (score.get('quality_status')=='valid' and score.get('input_contract_version')=='pit-input-v1'
            and score.get('source_feature_updated_at')==feature.get('updated_at')
            and score.get('source_factor_version')==score.get('version')
            and score.get('percentile') is not None):
            valid_names.append(score['factor_name'])
    source={'feature':feature,'scores':scores}
    fingerprint=hashlib.sha256(json.dumps(source,sort_keys=True,default=str).encode()).hexdigest()
    return {'version':'snapshot-input-v1','input_contract_version':feature.get('input_contract_version'),
        'knowledge_mode':feature.get('knowledge_mode'),'strict_pit_ready':strict,
        'decision_cutoff':feature.get('decision_cutoff'),'feature_updated_at':feature.get('updated_at'),
        'eligible_score_names':sorted(valid_names),'input_sha256':fingerprint}
