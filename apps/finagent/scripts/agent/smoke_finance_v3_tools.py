"""Bounded probes of the tools actually exposed to the production chat Agent.

No LLM calls, chat messages, portfolio writes or historical full-table scans.
"""
import datetime as dt
import json
from pathlib import Path
import sys,time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'MMClaw'))
from mmclaw.tools import FinanceDBTool as DB,SafeCalculatorTool


def _graph_meta_contract(payload):
    meta=payload.get('meta',{})
    return (meta.get('read_only') is True and meta.get('causal_interpretation_forbidden') is True
      and meta.get('coverage_pct') is None and bool(meta.get('decision_cutoff'))
      and bool(meta.get('world_time')) and meta.get('row_count')==len(payload.get('rows',[])))


def graph_supported_rows(payload):
    """A live graph response must retain review, time and evidence boundaries."""
    if not _graph_meta_contract(payload) or not payload.get('rows') or payload['meta'].get('status')!='ok':
        return False
    try:
        cutoff=dt.datetime.fromisoformat(payload['meta']['decision_cutoff'])
        day=dt.date.fromisoformat(payload['meta']['world_time'])
        if cutoff.tzinfo is None:return False
        for row in payload['rows']:
            attrs=row.get('attributes') or {}
            if (row.get('status')!='active' or row.get('review_status') not in ('approved','classified')
              or str((attrs.get('attribution_check') or {}).get('supported',True)).lower()=='false'
              or str(attrs.get('superseded_by_canonical',False)).lower()=='true'
              or row.get('claim_class') not in ('observed_fact','extracted_claim','model_inference')):
                return False
            if (dt.date.fromisoformat(row['valid_from'])>day
              or (row.get('valid_to') and dt.date.fromisoformat(row['valid_to'])<=day)
              or dt.datetime.fromisoformat(row['public_known_from'])>cutoff
              or (row.get('public_known_to') and dt.datetime.fromisoformat(row['public_known_to'])<=cutoff)):
                return False
            if row.get('evidence_public_at') and dt.datetime.fromisoformat(row['evidence_public_at'])>cutoff:
                return False
            if row['claim_class']=='observed_fact' and (row['review_status']!='approved'
                or not any(source in row.get('source_system','').lower() for source in ('cninfo','sse','szse','bse','official'))):
                return False
        return True
    except (KeyError,TypeError,ValueError):
        return False


def graph_official_evidence_contract(payload):
    """Mandatory positive fixture: 688825 has reviewed official product evidence.

    This must fail on an empty response. The separate theme boundary is never a
    substitute for exercising successful, evidence-backed graph retrieval.
    """
    if not graph_supported_rows(payload):return False
    for row in payload['rows']:
        attrs=row.get('attributes') or {}
        if (attrs.get('stock_code')=='688825' and row.get('review_status')=='approved'
          and row.get('source_system')=='cninfo_official' and row.get('claim_class')=='observed_fact'
          and row.get('source_document_id') and row.get('evidence_text') and attrs.get('interpretation_limit')):
            for evidence in payload.get('evidence',[]):
                value=evidence.get('value') or {}
                if (evidence.get('claim_type')=='graph_association' and evidence.get('quality_status')=='verified'
                  and evidence.get('source_document_id')==row['source_document_id']
                  and evidence.get('source_id')==row['source_system']
                  and evidence.get('entity_id')==row['source_entity_id']
                  and evidence.get('metric_id')==row['relation_type']
                  and value.get('target_entity_id')==row['target_entity_id']
                  and value.get('evidence_text')==row['evidence_text']
                  and value.get('claim_class')=='observed_fact' and value.get('review_status')=='approved'):
                    return True
    return False


def graph_absence_contract(payload):
    """No eligible reviewed edges means explicit unavailable, without invented facts."""
    meta=payload.get('meta',{})
    return (_graph_meta_contract(payload) and payload.get('rows')==[] and payload.get('evidence')==[]
      and meta.get('status')=='unavailable' and meta.get('quality_status')=='fail'
      and meta.get('data_through') is None and meta.get('paths')==[] and meta.get('shared_research')=={})


def graph_original_theme_contract(payload):
    # 光模块 currently has only unreviewed/rejected versions (audit 2026-10-13).
    # A later genuinely reviewed edge may be returned, but candidate leakage must fail.
    return graph_supported_rows(payload) if payload.get('rows') else graph_absence_contract(payload)


def main():
    today=dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    rows=[];payloads={}
    def check(name,call,validate):
        started=time.monotonic()
        try:
            raw=call()
            if str(raw).startswith(('FinanceDB error:','Error:')):raise RuntimeError(str(raw)[-1400:])
            payload=json.loads(raw)
            passed=bool(validate(payload));error=None
            payloads[name]=payload
        except Exception as exc:
            payload={};passed=False;error=f'{type(exc).__name__}: {exc}'[:1000]
        rows.append({'tool':name,'passed':passed,'seconds':round(time.monotonic()-started,3),
          'status':payload.get('meta',{}).get('status'),'row_count':payload.get('meta',{}).get('row_count'),'error':error})
    has_rows=lambda p: bool(p.get('rows')) and p.get('meta',{}).get('status') not in ('error','invalid_request')
    check('time',lambda:DB.time_context(),has_rows)
    check('entities',lambda:DB.resolve_entities(query='美的集团',limit=2),has_rows)
    check('status',lambda:DB.data_status(limit=10),has_rows)
    check('market',lambda:DB.market_series_batch(stock_codes=['000333'],start_date=str(today-dt.timedelta(days=30)),end_date=str(today),limit=20),has_rows)
    check('screen',lambda:DB.market_screen_v3(stock_codes=['000333'],limit=2),has_rows)
    check('fundamentals',lambda:DB.fundamentals_batch(stock_codes=['000333'],metric_codes=['revenue','net_profit_parent'],periods=1),
      lambda p:has_rows(p) and all(r['quality_status']=='verified' for r in p['rows']))
    check('factor',lambda:DB.factor_analysis(stock_code='300308',limit=3),
      lambda p:p.get('meta',{}).get('score_input_contract')=='pit-input-v1' and
        (has_rows(p) or (p.get('meta',{}).get('status')=='unavailable' and p['meta'].get('availability_reason')=='no_eligible_score_for_cutoff' and not p.get('rows'))))
    check('factor_validation',lambda:DB.factor_analysis(mode='validation',limit=3),lambda p:has_rows(p) and 'forecast_model_validation' in p['meta'])
    check('graph_official_evidence',lambda:DB.graph_analysis(stock_codes=['688825'],
      relation_types=['PRODUCES','USES'],limit=5),graph_official_evidence_contract)
    check('graph_original_theme_boundary',lambda:DB.graph_analysis(query='光模块',limit=3),graph_original_theme_contract)
    check('focused_bundle',lambda:DB.research_bundle(workflow_type='company_due_diligence',stock_code='000333',components=['market','fundamentals'],limit=3),
      lambda p:has_rows(p) and set(p['meta']['selected_components'])=={'market','fundamentals'} and len(p['rows'])<=6)
    facts=payloads.get('fundamentals',{}).get('rows',[])
    doc=next((r['source_document_id'] for r in facts if r.get('source_document_id')),None)
    check('document_read',lambda:DB.document_retrieve_v3(document_id=doc,max_lines=20),lambda p:has_rows(p) and p['meta']['mode']=='read' and bool(p['rows'][0]['text']))
    check('calculator',lambda:SafeCalculatorTool.calculate(expression='pct_change(new, old)',variables={'new':110,'old':100}),
      lambda p:float(p.get('result',p.get('rounded_result',-1)))==0.1)
    result={'passed':all(r['passed'] for r in rows),'checks':rows}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
