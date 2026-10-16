"""Small read-only projection shared by Agent and daily-report evidence."""
from __future__ import annotations

def load_plans(conn,cutoff,as_of=None,codes=None,limit=10):
    if not conn.execute("SELECT to_regclass('quant.daily_plan_runs') present").fetchone()['present']:
        return {'status':'not_installed','rows':[]}
    run=conn.execute('''SELECT run_id,report_date,market_date,decision_at,mode,summary
       FROM quant.daily_plan_runs WHERE decision_at<=%s AND report_date<=%s
       ORDER BY decision_at DESC LIMIT 1''',(cutoff,as_of or cutoff.date())).fetchone()
    if not run:return {'status':'not_yet_generated','rows':[]}
    rows=conn.execute('''SELECT payload FROM quant.daily_plan_revisions WHERE run_id=%s
      AND (%s::text[] IS NULL OR stock_code=ANY(%s)) ORDER BY stock_code LIMIT %s''',
      (run['run_id'],codes,codes,max(1,min(int(limit),20)))).fetchall()
    items=[]
    for r in rows:
        p=r['payload'];approved=run['mode']=='approved'
        items.append({'component':'candidate_pool_trade_plan','stock_code':p['stock_code'],'name':p.get('name'),
            'as_of_date':str(run['market_date']),'available_at':run['decision_at'].isoformat(),
            'plan_date':str(run['report_date']),'mode':'已通过验收的计划' if approved else '模拟观察记录',
            'state':p['state'],'change':p['change'],'reasons':p['reason'],'forward_pe':p.get('forward_pe'),
            'eps_consensus':(p.get('evidence') or {}).get('eps_median'),
            'eps_institutions':(p.get('evidence') or {}).get('institutions'),
            'valuation_scenarios':p.get('valuation'),'valuation_meaning':'同业估值情景；不等于预测涨幅或到达日期',
            'entry_limit':p.get('entry_limit') if approved else None,
            'entry_expires_on':p.get('entry_expires_on') if approved else None,
            'expected_net_return':p.get('expected_net_return') if approved else None,
            'expected_sell_date':p.get('expected_sell_date') if approved else None,
            'cost_break_even':p.get('cost_break_even'),'candidate_membership_is_position':False,
            'paper_quantity':p.get('paper_quantity',0),'position_scope':'模拟参考组合，不代表群成员实际持仓'})
    return {'status':'available','summary':run['summary'],'rows':items}
