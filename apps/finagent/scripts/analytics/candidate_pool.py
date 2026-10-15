"""Persistent research discovery and scheduled competition, independent of trading."""
from __future__ import annotations
import datetime as dt
import json
import math
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION='candidate-pool-v3'
TZ=ZoneInfo('Asia/Shanghai')
CONFIG_PATH=Path(__file__).resolve().parents[2]/'config/research_pool.json'
TIMING={'momentum_20d','reversal_5d','low_volatility_20d','volume_surprise_20d','drawdown_20d','money_flow_intensity_5d'}

def config(path=None):
    c=json.loads(Path(path or CONFIG_PATH).read_text())
    if not 1<=c['core_capacity']<=c['watch_capacity']<=c['exploration_capacity']<=1000:raise ValueError('invalid pool capacities')
    for key in ('weekly_replacements','industry_core_limit','minimum_hold_days','cooling_days','filing_lookback_days','filings_per_company','filing_limit','learning_stock_limit'):
        if not isinstance(c[key],int) or c[key]<1:raise ValueError('invalid '+key)
    if c['learning_stock_limit']>20:raise ValueError('learning sample limit is 20')
    if not 0<=c['replacement_margin']<=50:raise ValueError('invalid replacement margin')
    return c

def number(x):
    try:
        n=float(x)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None

def day(x):return dt.date.fromisoformat(str(x)[:10])
def clipped(x,lo,hi):return min(hi,max(lo,x))

def score_candidate(p,as_of,feedback=None):
    """Only verified financial values affect the long-term score; counts are recall only."""
    market=p.get('market') or {};facts=p.get('actual_financials') or []
    verified=[f for f in facts if f.get('quality_status')=='verified' and f.get('period_end') and day(f['period_end'])<=as_of]
    recent=[f for f in verified if (as_of-day(f['period_end'])).days<=210]
    metrics={f['metric_code']:f for f in sorted(recent,key=lambda f:str(f['period_end']))}
    components=[];thesis=[];financial_evidence=[]
    for name,label in [('revenue','营收'),('net_profit_parent','归母净利润')]:
        f=metrics.get(name+'_yoy') or metrics.get(name)
        growth=number(f.get('value') if f and f.get('metric_code')==name+'_yoy' else f.get('yoy') if f else None)
        if growth is not None:
            components.append(clipped(growth/50,-1,1))
            thesis.append(f"{f['period_end']} {label}同比 {growth:.2f}%，来自已核实披露。")
            financial_evidence.append({'metric':label+'同比','period_end':str(f['period_end']),
                'value':round(growth,2),'unit':'%','quality_status':'verified','basis':'已核实披露'})
    cash=metrics.get('operating_cash_flow');profit=metrics.get('net_profit_parent')
    if cash and profit and cash['period_end']==profit['period_end'] and cash.get('unit')==profit.get('unit') and (number(profit.get('value')) or 0)>0:
        ratio=number(cash.get('value'))/float(profit['value']) if number(cash.get('value')) is not None else None
        if ratio is not None:
            components.append(clipped(ratio-1,-1,1));thesis.append(f"同报告期经营现金流／归母净利润为 {ratio:.2f}。")
            financial_evidence.append({'metric':'经营现金流／归母净利润','period_end':str(cash['period_end']),
                'value':round(ratio,2),'unit':'倍','quality_status':'verified','basis':'同报告期、同币种的已核实数值计算'})
    long_term=round(50+50*sum(components)/len(components),2) if components else None
    profitable=bool(profit and (number(profit.get('value')) or 0)>0)
    financial_risks=[]
    if profit and not profitable:financial_risks.append('最近已核实报告期归母净利润非正，同比分数不能替代盈利核查')
    if cash and (number(cash.get('value')) or 0)<0:financial_risks.append('最近已核实经营现金流为负，需核对营运资金与季节性')
    factors=[f for f in p.get('factors',[]) if f.get('factor_name') in TIMING and f.get('dt')==str(as_of) and number(f.get('percentile')) is not None]
    timing=round(100*sum(float(f['percentile']) for f in factors)/len(factors),2) if factors else None
    # Classified graph edges support discovery; only approved signed evidence can
    # influence direction. Generic filings never become a bullish count signal.
    graph=p.get('graph') or []
    signed=[g for g in graph if g.get('review_status')=='approved' and number(g.get('direction')) in (-1,1)]
    catalyst=clipped(sum(float(g['direction'])*float(g.get('confidence') or 0) for g in signed),-1,1)
    penalty=0.;feedback_state='尚未纳入本轮反馈检查' if feedback is None else '尚无成熟反馈'
    if feedback and feedback.get('availability')=='unavailable':feedback_state='反馈暂不可读取'
    elif feedback and feedback.get('mature_observations'):feedback_state=f"已记录 {feedback['mature_observations']} 条到期结果，按独立窗口持续复核"
    if feedback and feedback.get('review_required'):
        penalty=5.;feedback_state='成熟预测误差触发复核，短期排序降权'
    available=[(long_term,.65),(timing,.35)]
    values=[(v,w) for v,w in available if v is not None]
    # Missing timing/fundamentals must not improve a company's ranking by
    # redistributing their weight to its one favorable observed component.
    score=sum(v*w for v,w in values)
    score=round(clipped(score+5*catalyst-penalty,0,100),2)
    quality=market.get('quality_status') in ('verified','unverified') and (number(market.get('close')) or 0)>0
    liquid=(number(market.get('amount')) or 0)>=20_000_000 or (market.get('amount') is None and (number(market.get('liquidity_proxy_percentile')) or 0)>=.5)
    risk=bool(p.get('lifecycle_status') in ('delisted','suspended') or 'ST' in str(p.get('stock_name','')).upper())
    channels=[]
    if long_term is not None:channels.append('财务变化')
    if timing is not None:channels.append('量价因子')
    if graph or p.get('theme'):channels.append('业务关联线索')
    if p.get('filings'):channels.append('新公告待分析')
    if not thesis:thesis=['长期财务论点待补充已核实且同口径的数据。']
    return {'stock_code':p['stock_code'],'name':p.get('stock_name') or p['stock_code'],
        'industry':p.get('industry_name') or None,'score':score,'long_term_score':long_term,'timing_score':timing,
        'score_meaning':'可解释研究排序分，非收益率或上涨概率','channels':channels,
        'eligible':quality and liquid and not risk,'core_eligible':quality and liquid and not risk and len(components)>=2 and long_term>=50 and timing is not None and profitable,
        'financial_risks':financial_risks,
        'score_input_coverage':sum(w for _,w in values),
        'financial_components':len(components),'thesis':thesis,'financial_evidence':financial_evidence,'catalyst_direction':catalyst,
        'feedback_adjustment':-penalty,'feedback_status':feedback_state,
        'financials_as_of':max((str(f['period_end']) for f in recent),default=None),
        'market_as_of':str(market.get('dt') or as_of),'source_quality':market.get('quality_status'),
        'filings':p.get('filings',[])[:3],
        'graph_leads':[{'source':g.get('source_label'),'relation':g.get('relation_type'),'target':g.get('target_label'),
                        'review_status':g.get('review_status'),'evidence':g.get('evidence_text')} for g in graph[:2]],
        'next_checks':['核对新公告正文的具体业务影响','核对下一期收入、利润与经营现金流','比较 5 日和 20 日冻结预测的到期结果'],
        'exclusion_reason':('证券状态不适合纳入当前池' if risk else '行情或流动性待核实' if not (quality and liquid) else None)}

def evolve(rows,previous,as_of,now,cfg,previous_run=None):
    """Daily recall, weekly bounded competition, monthly thesis review."""
    today=now.astimezone(TZ).date();week=f'{today.isocalendar().year}-W{today.isocalendar().week:02d}';month=today.strftime('%Y-%m')
    weekly=not previous_run or previous_run['weekly_key']!=week or previous_run.get('version',VERSION)!=VERSION
    monthly=not previous_run or previous_run['monthly_key']!=month
    old={r['stock_code']:r for r in previous};all_rows={r['stock_code']:dict(r) for r in rows}
    ordered=sorted((r for r in rows if r['eligible']),key=lambda r:(-r['score'],r['stock_code']))
    # Round-robin distinct discovery channels prevents one filing batch from
    # occupying every exploration slot. Retain ongoing research first.
    retained=[c for c,p in old.items() if p['state'] in ('core','watching') and c in all_rows]
    selected=list(dict.fromkeys(retained));streams=[]
    for channel in ('财务变化','量价因子','业务关联线索','新公告待分析'):
        items=[r for r in ordered if channel in r['channels']]
        if channel=='财务变化':items.sort(key=lambda r:(-(r['long_term_score'] or 0),r['stock_code']))
        if channel=='新公告待分析':items.sort(key=lambda r:(str((r.get('filings') or [{}])[0].get('published_at','')),r['stock_code']),reverse=True)
        streams.append(items)
    for pos in range(max((len(s) for s in streams),default=0)):
        for stream in streams:
            if pos<len(stream) and stream[pos]['stock_code'] not in selected:selected.append(stream[pos]['stock_code'])
            if len(selected)>=cfg['exploration_capacity']:break
        if len(selected)>=cfg['exploration_capacity']:break
    selected=selected[:cfg['exploration_capacity']]
    ranked=[r for r in ordered if r['stock_code'] in set(selected)]
    core=[c for c,p in old.items() if p['state']=='core' and c in all_rows and all_rows[c]['core_eligible']]
    watch=[c for c,p in old.items() if p['state'] in ('core','watching') and c in all_rows and all_rows[c]['eligible']]
    replaced=[];industries=Counter(all_rows[c]['industry'] for c in core)
    def industry_ok(r):return industries[r['industry']]<cfg['industry_core_limit']
    if weekly:
        for r in ranked:
            code=r['stock_code']
            if code in core or not r['core_eligible']:continue
            if len(core)<cfg['core_capacity'] and industry_ok(r):
                core.append(code)
                industries[r['industry']]+=1
            elif len(replaced)<cfg['weekly_replacements']:
                droppable=[c for c in core if c in old and (today-day(old[c]['entered_at'])).days>=cfg['minimum_hold_days']]
                if not droppable:continue
                worst=min(droppable,key=lambda c:(all_rows[c]['score'],c))
                same_industry=r['industry']==all_rows[worst]['industry']
                if r['score']>=all_rows[worst]['score']+cfg['replacement_margin'] and (industry_ok(r) or same_industry):
                    industries[all_rows[worst]['industry']]-=1
                    core.remove(worst);core.append(code);replaced.append(worst)
                    industries[r['industry']]+=1
        # The broader watch set also has bounded weekly churn; core membership
        # always has precedence over the watch-capacity budget.
        watch=list(dict.fromkeys(core+watch))[:cfg['watch_capacity']]
        # One replacement budget across core and watch, not four for each tier.
        drops=len(replaced)
        for r in ranked:
            code=r['stock_code']
            if code in watch:continue
            if len(watch)<cfg['watch_capacity']:watch.append(code);continue
            movable=[c for c in watch if c not in core and c in old and (today-day(old[c]['entered_at'])).days>=cfg['minimum_hold_days']]
            if not movable or drops>=cfg['weekly_replacements']:continue
            worst=min(movable,key=lambda c:(all_rows[c]['score'],c))
            if r['score']>=all_rows[worst]['score']+cfg['replacement_margin']:
                watch.remove(worst);watch.append(code);drops+=1
    result=[]
    for code in sorted(set(selected)|set(old)):
        prev=old.get(code);r=all_rows.get(code)
        if not r:
            r={**prev,'eligible':False,'core_eligible':False,'exclusion_reason':'当前快照缺失，等待数据复核'}
        r=dict(r)
        state='core' if code in core else 'watching' if code in watch else 'exploring' if code in selected and r['eligible'] else 'cooling'
        # Rows dropped from the 300-name recall remain in immutable history;
        # expire their current cooling record after the configurable period.
        if state=='cooling' and prev and (today-day(prev.get('cooling_since') or today)).days>=cfg['cooling_days']:state='archived'
        if prev and prev['state']=='archived' and state=='cooling':continue
        r['state']=state;r['previous_state']=prev['state'] if prev else None
        r['change']='added' if not prev else 'state_changed' if prev['state']!=state else 'retained'
        r['entered_at']=prev.get('entered_at') if prev and prev['state']==state else today.isoformat()
        r['first_seen_at']=prev.get('first_seen_at') if prev else now.isoformat()
        r['cooling_since']=(prev.get('cooling_since') or today.isoformat()) if state=='cooling' and prev else today.isoformat() if state=='cooling' else None
        r['rank_reason']=r.get('exclusion_reason') or ('新证据与综合排序进入本轮研究名单' if r['change']=='added' else
            '达到周度替换条件，调整研究优先级' if r['change']=='state_changed' else '继续跟踪，保持期内保留研究连续性')
        if monthly or not prev:
            r['monthly_review']={'reviewed_at':now.isoformat(),'thesis':r['thesis'],'financials_as_of':r['financials_as_of'],
                'change':'首次建立长期论点' if not prev else '披露指标或研究论点更新' if prev.get('monthly_review',{}).get('thesis')!=r['thesis'] else '长期论点未发现新增变化',
                'next_review_date':(today.replace(day=28)+dt.timedelta(days=4)).replace(day=1).isoformat()}
        else:r['monthly_review']=prev.get('monthly_review')
        result.append(r)
    result.sort(key=lambda r:({'core':0,'watching':1,'exploring':2,'cooling':3,'archived':4}[r['state']],-r['score'],r['stock_code']))
    for rank,r in enumerate(result,1):r['rank']=rank
    return result,{'weekly_key':week,'monthly_key':month,'weekly_rebalanced':weekly,'monthly_reviewed':monthly,
        'state_counts':dict(Counter(r['state'] for r in result)),'new_candidates':sum(r['change']=='added' for r in result),
        'state_changes':sum(r['change']=='state_changed' for r in result),'core_replacements':len(replaced),
        'source_stock_count':len(rows),'unknown_industry_count':sum(not r['industry'] for r in result),
        'production_approved':False}

def load_pool(conn,cutoff,as_of=None,stock_codes=None,limit=10):
    from scripts.analytics.forecast_learning import VERSION as LEARNING_VERSION
    from scripts.analytics.forecast_evidence import validation_evidence
    if not conn.execute("SELECT to_regclass('analytics.candidate_pool_runs') present").fetchone()['present']:
        return {'status':'unavailable','rows':[]}
    run=conn.execute('''SELECT * FROM analytics.candidate_pool_runs WHERE created_at<=%s AND decision_cutoff<=%s
        AND (%s::date IS NULL OR as_of_date<=%s::date) ORDER BY created_at DESC LIMIT 1''',(cutoff,cutoff,as_of,as_of)).fetchone()
    if not run:return {'status':'unavailable','rows':[]}
    rows=conn.execute('''SELECT payload FROM analytics.candidate_pool_revisions WHERE run_id=%s AND state<>'archived'
        AND (%s::text[] IS NULL OR stock_code=ANY(%s)) ORDER BY rank LIMIT %s''',(run['run_id'],stock_codes,stock_codes,max(1,min(int(limit),40)))).fetchall()
    output=[]
    for record in rows:
        p=record['payload'];keys=('stock_code','name','state','rank','score','score_meaning','long_term_score','timing_score',
            'channels','thesis','financial_evidence','financial_risks','rank_reason','change','feedback_adjustment','feedback_status','financials_as_of','market_as_of','monthly_review','next_checks')
        record={k:p.get(k) for k in keys}
        record['channels']=['业务关联线索' if c=='已审核业务关联' else c for c in (record.get('channels') or [])]
        output.append(record)
    codes=[r['stock_code'] for r in output]
    forecasts=conn.execute('''SELECT DISTINCT ON(p.stock_code,p.horizon) p.stock_code,p.horizon,p.signal_date,p.entry_date,p.exit_date,
        p.created_at,p.expected_gross_return,p.interval_80,p.research_qualified FROM analytics.learning_predictions_v1 p
        JOIN analytics.learning_models_v1 m USING(model_id)
        WHERE p.stock_code=ANY(%s) AND p.created_at<=%s AND p.signal_date<=%s
          AND m.model_version=%s AND m.created_at<=%s
        ORDER BY p.stock_code,p.horizon,p.signal_date DESC,p.created_at ASC''',(codes,cutoff,as_of or cutoff.astimezone(TZ).date(),LEARNING_VERSION,cutoff)).fetchall() if codes else []
    models=conn.execute('''SELECT DISTINCT ON(horizon) horizon,as_of,created_at,evaluation
        FROM analytics.learning_models_v1 WHERE model_version=%s AND created_at<=%s AND as_of<=%s
        ORDER BY horizon,created_at DESC''',(LEARNING_VERSION,cutoff,as_of or cutoff.astimezone(TZ).date())).fetchall()
    forecast_validation=[validation_evidence(m) for m in models]
    for r in output:
        company_forecasts=[f for f in forecasts if f['stock_code']==r['stock_code']]
        r['as_of_date']=str(max([run['as_of_date']]+[f['signal_date'] for f in company_forecasts]))
        r['available_at']=max([run['decision_cutoff']]+[f['created_at'] for f in company_forecasts]).astimezone(TZ).isoformat()
        r['forecasts']=[{'horizon_trading_days':f['horizon'],'signal_date':str(f['signal_date']),'entry_date':str(f['entry_date']),
            'exit_date':str(f['exit_date']),'status':'研究检验通过，继续记录实际结果' if f['research_qualified'] else '预测已冻结，模型误差仍需改善',
            'expected_return_percent':100*f['expected_gross_return'] if f['research_qualified'] else None,
            'interval_80_percent':[100*x for x in f['interval_80']] if f['research_qualified'] else None}
            for f in company_forecasts]
    mined=conn.execute('''SELECT as_of_date,created_at,summary FROM analytics.factor_reconstruction_runs
        WHERE created_at<=%s AND as_of_date<=%s ORDER BY created_at DESC LIMIT 1''',(cutoff,as_of or cutoff.astimezone(TZ).date())).fetchone()
    mining=None
    if mined:
        labels={'return_5d':'5日涨跌幅','return_20d':'20日涨跌幅','volatility_20d':'20日波动率',
            'drawdown_20d':'20日回撤','volume_ratio_20d':'20日量比','_plus_':' + ','_minus_':' − '}
        def readable_fold(f):
            result={k:f.get(k) for k in ('train_start','train_end','test_start','test_end','training_dates','test_dates',
                'test_rank_ic','test_rank_ic_interval','legacy_test_rank_ic','test_top_bottom_spread','costs_included','purpose')}
            expression=f.get('chosen_expression') or '无稳定候选表达式'
            for raw,label in labels.items():expression=expression.replace(raw,label)
            result['factor_description']=expression
            result['direction_text']='原方向' if f.get('direction')==1 else ('反方向' if f.get('direction')==-1 else '未选择')
            result['selected_factor_count']=len(f.get('selected_factors',[]))
            bt=f.get('backtest') or {}
            result['cost_backtest']={k:bt.get(k) for k in ('holding_windows','cost_bps_round_trip','strategy',
                'equal_weight','mean_window_excess','executable_result','limitations')}
            return result
        mining={'as_of_date':str(mined['as_of_date']),'updated_at':mined['created_at'].astimezone(TZ).isoformat(),
            'status':mined['summary']['status'],'mode':'历史重构研究','production_approved':False,
            'history_configuration':{'adjustable':True,'minimum_training_sessions_range':[60,2000],
                'test_sessions_range':[20,252],'folds_range':[1,5],
                'method':'可配置最短训练段、每折测试期和折数；训练段可使用更长历史，样本不足不会自动降低门槛。',
                'required_sessions_formula':'最短训练段 + 折数 ×（max(配置测试期, 2 × 预测期) + 预测期）'},
            'horizons':[{'horizon':r['horizon'],'status':r['status'],'eligible_sessions':r['eligible_sessions'],
                'required_sessions':r['required_sessions'],'history_plan':r['plan'],
                'research_signal_validated':r.get('research_signal_validated',False),
                'folds':[readable_fold(f) for f in r['folds']]} for r in mined['summary']['results']]}
    return {'status':'available','as_of_date':str(run['as_of_date']),'decision_cutoff':run['decision_cutoff'].isoformat(),
        'summary':run['summary'],'mining':mining,'forecast_validation':forecast_validation,
        'forecast_validation_updated_at':max((m['created_at'] for m in models),default=None),
        'rows':output,'references':{'run_id':str(run['run_id'])}}
