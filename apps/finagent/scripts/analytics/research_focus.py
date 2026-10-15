"""Evidence-led research attention. Pure policy plus a bounded read interface.

This module never approves predictions, sends messages, or places orders.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import re
from collections import defaultdict
from zoneinfo import ZoneInfo

VERSION='research-focus-v1'
TZ=ZoneInfo('Asia/Shanghai')
WEIGHTS={'official_disclosure':28.,'broker_opinion':18.,'financial_actual':24.,'graph_lead':8.}
LABELS={'official_disclosure':'公司公告','broker_opinion':'机构研报','financial_actual':'已核实财务','graph_lead':'已归属的主题线索'}
DENIED={'candidate','rejected','archived','conflict','template_noise','parse_failed','quarantined','unreviewed'}

def serial(value):
    return json.loads(json.dumps(value,default=str,ensure_ascii=False))

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def timestamp(value):
    if not value:return None
    if isinstance(value,dt.date) and not isinstance(value,dt.datetime):value=dt.datetime.combine(value,dt.time(),TZ)
    if isinstance(value,str):value=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=TZ)

def clean_evidence(raw,cutoff):
    """No future evidence; status wins over a caller's optimistic claim label."""
    out=[];excluded=defaultdict(int)
    for value in raw:
        e=dict(value);kind=e.get('kind');available=timestamp(e.get('available_at'));published=timestamp(e.get('published_at'))
        if kind not in WEIGHTS or not available or not published:
            excluded['missing_type_or_timestamp']+=1;continue
        if available>cutoff or published>cutoff:
            excluded['future_evidence']+=1;continue
        if str(e.get('status','active')) in DENIED or str(e.get('quality_status','accepted')) in DENIED:
            excluded['source_not_accepted']+=1;continue
        if kind=='graph_lead' and (e.get('status')!='active' or e.get('review_status') not in ('approved','classified') or not e.get('attribution_supported',False)):
            excluded['unreviewed_or_unsupported_relation']+=1;continue
        if kind in ('official_disclosure','financial_actual') and e.get('quality_status')!='verified':
            excluded['not_verified_primary_source']+=1;continue
        quality=float(e.get('source_quality',0))
        if not math.isfinite(quality) or quality<.45:
            excluded['source_quality_below_floor']+=1;continue
        e['source_quality']=min(1.,quality)
        e['claim_class']={'official_disclosure':'已发布公告（正文影响待核实）','financial_actual':'已核实披露数值',
                          'broker_opinion':'机构观点，不代表已发生事实','graph_lead':'提取的业务关联线索，需与原文核对'}[kind]
        e['published_at']=published.isoformat();e['available_at']=available.isoformat()
        normalized=re.sub(r'\s+','',str(e.get('title') or '')).lower()
        # Dedupe across syndicated feeds; retain document content version
        # separately so a revised original document is a material change.
        e['_identity']=fingerprint([e.get('stock_code'),kind,normalized,published.astimezone(TZ).date()])
        e['_content']=str(e.get('content_version') or e.get('document_id') or e['_identity'])
        out.append(e)
    chosen={};aliases=defaultdict(set);content_groups={}
    for e in sorted(out,key=lambda x:(-x['source_quality'],x.get('source_name',''),str(x.get('document_id','')))):
        content_key=(e.get('stock_code'),e['kind'],e.get('content_version'))
        key=content_groups.get(content_key,e['_identity']) if e.get('content_version') else e['_identity']
        aliases[key].add(str(e.get('document_id') or ''))
        chosen.setdefault(key,e)
        if e.get('content_version'):content_groups[content_key]=key
        merged={str(t['id']):t for t in [*(chosen[key].get('themes') or []),*(e.get('themes') or [])]}
        chosen[key]['themes']=list(merged.values())
    for key,e in chosen.items():e['deduplicated_sources']=len(aliases[key]-{''})
    excluded['duplicate_observations']=len(out)-len(chosen)
    return list(chosen.values()),dict(excluded)

def _evidence_score(e,cutoff):
    age=max(0.,(cutoff.astimezone(TZ).date()-timestamp(e['published_at']).astimezone(TZ).date()).days)
    # Publication age, not ingest age: historical backfill is not breaking news.
    return WEIGHTS[e['kind']]*e['source_quality']*2**(-age/7.)

def _checks(kinds,theme=False):
    checks=[]
    if 'official_disclosure' in kinds:checks.append('阅读公告正文，核对业务影响、披露口径及需继续跟踪的履行或进展事项。')
    if 'broker_opinion' in kinds:checks.append('对照原始公告核验机构观点，并比较不同机构的假设与分歧。')
    if 'financial_actual' in kinds:checks.append('比较同一合并口径与报告期间的财务数据，核查现金流和盈利是否一致。')
    if theme or 'graph_lead' in kinds:checks.append('逐家公司核对与主题的实际业务关系，区分直接业务、间接供应及仅被提及。')
    checks.append('下一次复核新披露与来源更新；收益模型未获准入时继续事实分析，不给出胜率或目标收益。')
    return checks

def build_focus(raw_evidence,contexts,previous,cutoff,*,as_of=None,max_active=12,max_watching=24):
    cutoff=timestamp(cutoff);as_of=as_of or cutoff.astimezone(TZ).date();as_of=str(as_of)
    evidence,excluded=clean_evidence(raw_evidence,cutoff);groups=defaultdict(list);labels={};codes=defaultdict(set)
    previous={p['focus_key']:p for p in previous}
    for e in evidence:
        code=str(e.get('stock_code') or '')
        if re.fullmatch(r'\d{6}',code):
            key='stock:'+code;groups[key].append(e);codes[key].add(code);labels[key]=contexts.get(code,{}).get('stock_name') or e.get('stock_name') or code
        for theme in e.get('themes') or []:
            key='theme:'+str(theme['id']);groups[key].append(e);labels[key]=str(theme['name']);codes[key].add(code)
    candidates={}
    for key in sorted(set(groups)|set(previous)):
        old=previous.get(key,{});entries=groups.get(key,[]);old_versions=old.get('_evidence_versions') or {}
        new=[e for e in entries if e['_identity'] not in old_versions]
        revised=[e for e in entries if e['_identity'] in old_versions and old_versions[e['_identity']]!=e['_content']]
        # Same source repeated cannot overwhelm multiple independent kinds.
        contributions=sorted((_evidence_score(e,cutoff) for e in entries),reverse=True)
        base=min(58.,sum(contributions[:3]));kinds={e['kind'] for e in entries}
        novelty=min(10.,len(new)*4.+len(revised)*6.)
        diversity=min(8.,max(0,len(kinds)-1)*4.)
        score=round(min(100.,base+novelty+diversity),2)
        entity_type='theme' if key.startswith('theme:') else 'stock'
        context=contexts.get(key.split(':',1)[1],{}) if entity_type=='stock' else {}
        reasons=[]
        for kind in WEIGHTS:
            count=sum(e['kind']==kind for e in entries)
            if count:reasons.append(f'当前资料窗口内有 {count} 份不重复的{LABELS[kind]}，按原始披露日期衡量时效。')
        if revised:reasons.append(f'{len(revised)} 份已有资料内容修订，需要重新核对原结论。')
        if old and new:reasons.append(f'较上次复核新增 {len(new)} 份资料。')
        if not old and entries:reasons.append('首次建立关注基线；已有历史资料不表述为今天新发生的事件。')
        if entity_type=='theme':reasons.append('按相关公司的披露与业务线索聚合；尚不能据此认定主题获得直接利好。')
        baseline=context.get('financial_baseline') or {};old_baseline=(old.get('context') or {}).get('financial_baseline') or {}
        changed_metrics=[name for name,value in baseline.items() if name in old_baseline and old_baseline[name]!=value]
        if changed_metrics:reasons.append('已核实财务记录发生更新：'+ '、'.join(changed_metrics[:4])+'；需区分新报告期与旧期修订。')
        feedback=context.get('forecast_feedback') or {}
        mature=int(feedback.get('mature_observations') or 0)
        # A temporary read failure/empty monitoring window must not erase the
        # last observed signature and later manufacture a "new" result.
        feedback_signature=fingerprint(feedback) if mature else old.get('_feedback_signature')
        feedback_changed=bool(old and mature and feedback_signature!=old.get('_feedback_signature'))
        if feedback:
            if feedback.get('availability')=='unavailable':
                reasons.append('成熟前瞻反馈当前无法读取；保留原关注，不将缺失记作 0 或负面结果。')
            elif not mature:
                reasons.append('真实冻结前瞻预测的成熟反馈为 0；不使用历史训练样本调整关注。')
            else:
                reasons.append(f'已有 {mature} 条真实冻结前瞻预测完成观察；仅用于复核预测误差与区间，不代表实际成交收益。')
                if feedback_changed:reasons.append('成熟前瞻反馈发生变化，研究记录已更新；不据此提高股票关注评分。')
                if not feedback.get('review_required'):
                    reasons.append('成熟反馈暂未触发复核规则；样本与重叠窗口限制见反馈详情，不据此宣称预测可靠。')
        if not entries:reasons.append('当前窗口没有新的合格资料；保留历史关注及待核查事项，避免把缺失误判为利空。')
        old_state=old.get('state');first=timestamp(old.get('first_seen_at')) or cutoff
        entered=timestamp(old.get('entered_active_at'))
        material=bool(new or revised or changed_metrics or feedback_changed)
        last_change=cutoff if material else timestamp(old.get('last_material_change_at')) or first
        observed_date=cutoff.astimezone(TZ).date().isoformat()
        prior_day=old.get('last_evaluated_date')
        low_days=(int(old.get('low_signal_days') or 0)+(1 if prior_day!=observed_date else 0)) if score<20 else 0
        held=old_state=='active' and entered is not None and (cutoff-entered)<dt.timedelta(hours=48)
        protected=old_state=='active' and (held or (score<20 and low_days<2))
        candidates[key]={'focus_key':key,'entity_type':entity_type,'label':labels.get(key) or old.get('label') or key,
            'stock_codes':sorted(c for c in codes.get(key,set(old.get('stock_codes') or [])) if c),
            'priority':score,'state':'watching','reasons':reasons,'next_checks':_checks(kinds,entity_type=='theme'),
            'evidence':sorted(entries,key=lambda e:(-_evidence_score(e,cutoff),e['_identity']))[:8],
            'evidence_count':len(entries),'first_seen_at':first.isoformat(),'entered_active_at':entered.isoformat() if entered else None,
            'last_material_change_at':last_change.isoformat(),'last_evaluated_date':observed_date,'low_signal_days':low_days,
            'new_evidence_count':len(new) if old else 0,'revised_evidence_count':len(revised),'financial_changes':changed_metrics,
            'feedback_changed':feedback_changed,'_feedback_signature':feedback_signature,
            'context':context,'analysis_mode':'evidence_tracking','prediction_eligible':False,
            'forecast_status':'研究关注可用；本状态不授予收益预测资格',
            '_evidence_versions':{e['_identity']:e['_content'] for e in entries},'_protected':protected,'_old_state':old_state}
        if feedback.get('review_required'):
            candidates[key]['next_checks']=list(dict.fromkeys([*feedback.get('next_checks',[]),*candidates[key]['next_checks']]))
    protected=sorted((p for p in candidates.values() if p['_protected']),key=lambda x:(-x['priority'],x['focus_key']))
    slots=max(0,max_active-len(protected));active={p['focus_key'] for p in protected}
    challengers=sorted((p for p in candidates.values() if p['focus_key'] not in active and (p['priority']>=32 or (p['_old_state']=='active' and p['priority']>=20))),
                       key=lambda x:(-(x['priority']+(6 if x['_old_state']=='active' else 0)),x['focus_key']))
    active.update(p['focus_key'] for p in challengers[:slots])
    output=[]
    for p in candidates.values():
        old=previous.get(p['focus_key'],{});old_state=p.pop('_old_state');p.pop('_protected')
        if p['focus_key'] in active:
            p['state']='active';p['entered_active_at']=p['entered_active_at'] if old_state=='active' else cutoff.isoformat()
        elif p['priority']>=12:p['state']='watching'
        elif old_state in ('active','watching','cooling') and (cutoff-timestamp(p['last_material_change_at']))<dt.timedelta(days=21):p['state']='cooling'
        else:p['state']='archived'
        if p['state']=='active' and old_state=='active' and p['priority']<32:p['reasons'].append('继续保留原关注：使用较低退出阈值与最短 48 小时观察期，避免反复进出。')
        p['change']='added' if not old else ('state_changed' if old_state!=p['state'] else ('evidence_updated' if p['new_evidence_count'] or p['revised_evidence_count'] or p['financial_changes'] else 'feedback_updated' if p['feedback_changed'] else 'retained'))
        p['previous_state']=old_state
        due=timestamp(old.get('next_review_at'))
        p['next_review_at']=(due if due and due>cutoff and p['change']=='retained' else
            cutoff+dt.timedelta(hours=24 if p['state']=='active' else 72 if p['state']=='watching' else 168)).isoformat()
        if p['feedback_changed'] and (p['context'].get('forecast_feedback') or {}).get('review_required'):
            p['next_review_at']=min(timestamp(p['next_review_at']),cutoff+dt.timedelta(hours=24)).isoformat()
        if not old and p['state']=='archived':continue
        if p['state']=='archived' and old_state=='archived' and not p['evidence']:continue
        output.append(p)
    output.sort(key=lambda p:({'active':0,'watching':1,'cooling':2,'archived':3}[p['state']],-p['priority'],p['focus_key']))
    # A challenger must beat an incumbent's retention bonus. Demotion retains
    # the old rationale as lower-frequency review; it never asserts invalidity.
    watch_pool=sorted((p for p in output if p['state']=='watching'),
        key=lambda p:(-(p['priority']+(4 if p['focus_key'] in previous else 0)),p['focus_key']))
    watch_keys={p['focus_key'] for p in watch_pool[:max_watching]}
    selected=[]
    for p in output:
        if p['state']=='watching' and p['focus_key'] not in watch_keys:
            if p['focus_key'] not in previous:continue
            p['state']='cooling';p['change']='state_changed'
            p['reasons'].append('其他对象的新证据优先级更高，本对象转为低频复核；不代表原业务论点已失效。')
            p['next_review_at']=(cutoff+dt.timedelta(hours=168)).isoformat()
        selected.append(p)
    selected.sort(key=lambda p:({'active':0,'watching':1,'cooling':2,'archived':3}[p['state']],-p['priority'],p['focus_key']))
    return serial(selected),{'version':VERSION,'as_of_date':as_of,'accepted_evidence':len(evidence),'excluded':excluded,
        'state_counts':{state:sum(p['state']==state for p in selected) for state in ('active','watching','cooling','archived')},
        'scope':'研究关注优先级，不是股票推荐排名或收益概率；文档窗口与候选上限见采集元数据。'}

def _public_row(row):
    p=row.get('payload',row);result={k:v for k,v in p.items() if not k.startswith('_') and k not in ('focus_key','context')}
    result['priority_meaning']='规则研究关注强度，不是模型概率或收益预期'
    result['state_label']={'active':'重点复核','watching':'持续观察','cooling':'降低优先级','archived':'已归档'}.get(p.get('state'),'待复核')
    result['context']={k:v for k,v in (p.get('context') or {}).items() if k!='financial_baseline'}
    public_evidence_keys={'title','source_name','url','published_at','available_at','stock_code','stock_name',
                          'claim_class','source_quality','body_chars','evidence_detail','evidence_excerpt','deduplicated_sources'}
    result['evidence']=[{**{k:v for k,v in e.items() if k in public_evidence_keys},'source_kind':LABELS.get(e.get('kind'),'来源资料')}
                        for e in p.get('evidence',[])]
    result['references']={'focus_key':p.get('focus_key'),'revision_id':str(row.get('revision_id') or ''),
                          'document_ids':[e.get('document_id') for e in p.get('evidence',[]) if e.get('document_id')]}
    return result

def load_focus(conn,cutoff,as_of=None,stock_codes=None,limit=10):
    """Read only persisted results known by cutoff, with private references apart."""
    cutoff=timestamp(cutoff);limit=max(1,min(int(limit),40))
    if not conn.execute("SELECT to_regclass('analytics.research_focus_runs') present").fetchone()['present']:
        return {'status':'unavailable','rows':[],'changes':[],'limitations':['研究关注模块尚未安装。']}
    run=conn.execute('''SELECT * FROM analytics.research_focus_runs
        WHERE created_at<=%s AND decision_cutoff<=%s AND (%s::date IS NULL OR as_of_date<=%s::date)
        ORDER BY created_at DESC,run_id DESC LIMIT 1''',(cutoff,cutoff,as_of,as_of)).fetchone()
    if not run:return {'status':'unavailable','rows':[],'changes':[],'limitations':['尚无截止时间前完成的研究关注更新。']}
    rows=conn.execute('''SELECT * FROM analytics.research_focus_revisions WHERE run_id=%s AND created_at<=%s
        AND (%s::text[] IS NULL OR stock_codes && %s::text[]) AND state<>'archived'
        ORDER BY CASE WHEN %s::text[] IS NOT NULL AND entity_type='stock' THEN 0 ELSE 1 END,
            CASE state WHEN 'active' THEN 0 WHEN 'watching' THEN 1 ELSE 2 END,priority DESC,focus_key LIMIT %s''',
        (run['run_id'],cutoff,stock_codes or None,stock_codes or None,stock_codes or None,limit)).fetchall()
    changes=conn.execute('''SELECT label,state,payload->>'previous_state' previous_state,payload->>'change' change
        FROM analytics.research_focus_revisions WHERE run_id=%s AND created_at<=%s
        AND (%s::text[] IS NULL OR stock_codes && %s::text[]) AND payload->>'change'<>'retained'
        ORDER BY priority DESC,focus_key LIMIT %s''',(run['run_id'],cutoff,stock_codes or None,stock_codes or None,limit)).fetchall()
    return {'status':'available','as_of_date':str(run['as_of_date']),'decision_cutoff':str(run['decision_cutoff']),
        'rows':[_public_row(r) for r in rows],'changes':serial(changes),
        'summary':{**{k:v for k,v in run['summary'].items() if k!='collection'},
          'collection':{k:v for k,v in (run['summary'].get('collection') or {}).items()
                        if k in ('as_of_date','knowledge_mode','lookback_days','source_windows','limits','source_truncated','source_counts')}},
        'references':{'run_id':str(run['run_id']),'snapshot_id':str(run['source_snapshot_id'] or '')},
        'limitations':['关注对象由证据质量、新颖性和时效选择；不等于通过量化准入的推荐股票。',
                       '已分类主题关系仅作为提取线索；机构研报仅代表来源观点。收益模型缺口不阻止事实研究。']}
