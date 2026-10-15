"""Shared immutable research read contract for daily reports and online tools."""
from __future__ import annotations
import datetime as dt

VERSION = "research-snapshot-v5"
WATCHLIST_NAME = "AI硬件研究观察池"

def load_snapshot(conn, cutoff, codes=None, as_of=None, limit=20):
    run = conn.execute("""SELECT * FROM analytics.research_snapshot_runs
        WHERE status='complete' AND version=%s AND decision_cutoff<=%s AND completed_at<=%s
          AND (%s::date IS NULL OR as_of_date<=%s::date)
        ORDER BY as_of_date DESC,decision_cutoff DESC LIMIT 1""",(VERSION,cutoff,cutoff,as_of,as_of)).fetchone()
    if not run:
        return {"status":"unavailable","reason":"尚无符合当前数据契约且在本次截止时间前完成的研究快照","rows":[],"watchlist":[]}
    rows = conn.execute("""SELECT payload FROM analytics.research_snapshot_daily
        WHERE snapshot_id=%s AND (%s::text[] IS NULL OR stock_code=ANY(%s))
        ORDER BY stock_code LIMIT %s""",(run['snapshot_id'],codes or None,codes or None,limit)).fetchall() if codes else []
    watchlist = conn.execute("""SELECT d.stock_code,d.decision,d.rank,d.reason,d.reference_price,s.payload
        FROM agent.watchlist_decisions d JOIN analytics.research_snapshot_daily s
          ON s.snapshot_id=d.snapshot_id AND s.stock_code=d.stock_code
        WHERE d.snapshot_id=%s AND d.decision IN ('added','retained')
          AND (%s::text[] IS NULL OR d.stock_code=ANY(%s))
        ORDER BY d.rank LIMIT %s""",(run['snapshot_id'],codes or None,codes or None,limit)).fetchall()
    removed = conn.execute("""SELECT stock_code,reason FROM agent.watchlist_decisions
        WHERE snapshot_id=%s AND decision='removed' ORDER BY stock_code""",(run['snapshot_id'],)).fetchall()
    framework=conn.execute("""SELECT as_of_date,checked_at,status,checks FROM quant.readiness_runs
        WHERE checked_at<=%s ORDER BY checked_at DESC LIMIT 1""",(cutoff,)).fetchone()
    paper=conn.execute("""SELECT s.session_date,s.status,s.payload,n.nav,n.cash,n.turnover,n.benchmark_close
        FROM quant.paper_sessions s LEFT JOIN quant.paper_nav n USING(account_id,session_date)
        WHERE s.completed_at<=%s AND s.account_id='research-mainboard-weekly-v1'
        ORDER BY s.session_date DESC LIMIT 1""",(cutoff,)).fetchone()
    return {"quant_framework":dict(framework) if framework else {'status':'not_yet_checked'},
        'paper_portfolio':{'mode':'模拟研究','latest':dict(paper) if paper else None,
            'usage':'按可执行约束估算的模拟账本；不是实盘成交或未来收益承诺'},
        "status":"available","version":run["version"],"snapshot_id":str(run['snapshot_id']),"as_of_date":run['as_of_date'],
        "decision_cutoff":run['decision_cutoff'],"quality":run['quality'],
        "rows":[r['payload'] for r in rows],"watchlist":list(watchlist),"removed":list(removed),
        "usage":"研究观察池；情景仅在模型及因子通过验证且获人工批准后用于预测。"}

def select_watchlist(payloads, limit=10):
    candidates=[]
    for p in payloads:
        market=p['market']; factors=p['factors']; reasons=[]
        valid=[f for f in factors if f.get('percentile') is not None and f['dt']==p['as_of_date']]
        coverage=len(valid)/p['expected_factors'] if p['expected_factors'] else 0
        if (not p.get('theme') or not ((market.get('amount') or 0)>=20_000_000 or (market.get('amount') is None and (market.get('liquidity_proxy_percentile') or 0)>=.5)) or
            market.get('quality_status') in ('rejected','conflict','incomplete') or coverage<.6):
            continue
        # Equal-weight rank is an explainable research screen, not a trained return forecast.
        approved=[f for f in valid if f.get('prediction_eligible')]
        used=approved or valid
        score=sum(float(f['percentile']) for f in used)/len(used)
        top=sorted(used,key=lambda f:f['percentile'],reverse=True)[:3]
        reasons.append({"kind":"factor_rank","text":"同日因子横截面排名","factors":[
            {"name":f['display_name'],"percentile":f['percentile'],"status":f['lifecycle_status']} for f in top]})
        reasons.append({"kind":"theme","text":"AI硬件主题存在研究证据","evidence":p['theme']})
        candidates.append((score,p['stock_code'],p,reasons,coverage,bool(approved)))
    return sorted(candidates,key=lambda x:(-x[0],x[1]))[:limit]


def compact_research(snapshot):
    """Retain sources/dates and numeric evidence, bound repeated internals for LLMs."""
    decisions={r['stock_code']:r for r in snapshot.get('watchlist',[])}
    def compact(p):
        membership=decisions.get(p['stock_code'])
        scenarios=[{**r,'validation':{k:v for k,v in r.get('validation',{}).items() if k!='folds'}} for r in p.get('scenarios',[])]
        priority={'revenue':0,'net_profit_parent':1,'operating_cash_flow':2,'eps_basic':3}

        return {"stock_code":p['stock_code'],"stock_name":p['stock_name'],"as_of_date":p['as_of_date'],
            "market":{k:p['market'].get(k) for k in ('close','source','quality_status','price_basis','liquidity_basis')},
            "actual_financials":sorted(p['actual_financials'],key=lambda r:priority.get(r['metric_code'],10))[:4],
            "financial_ratios":[{k:v for k,v in r.items() if k!='asset_id'} for r in p.get('financial_ratios',[])],
            "factors":[{k:v for k,v in f.items() if k in ('factor_name','display_name','family','lifecycle_status',
                'version','prediction_eligible','dt','raw_value','percentile','zscore','available_at')}
                for f in sorted(p['factors'],key=lambda f:f.get('percentile') or 0,reverse=True)[:4]],
            "factor_coverage":{"available":len(p['factors']),"expected":p['expected_factors']},
            "graph":p['graph'][:2],"graph_changes":p.get('graph_changes'),
            "broker_targets":p['broker_targets'][:2],"scenarios":scenarios,"limitations":p['limitations'],
            "watchlist_membership":({'selected':True,'decision':membership['decision'],'rank':membership['rank'],'reason':membership['reason']}
                if membership else {'selected':False,'reason':'未列入本次冻结的研究观察名单；不等于缺少数据或否定投资价值'})}
    return {**{k:v for k,v in snapshot.items() if k not in ('rows','watchlist')},
        'rows':[compact(p) for p in snapshot.get('rows',[])],
        'watchlist':[{**{k:v for k,v in row.items() if k!='payload'},'research':compact(row['payload'])}
                     for row in snapshot.get('watchlist',[])]}
