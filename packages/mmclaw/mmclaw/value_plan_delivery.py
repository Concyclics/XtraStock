"""Deliver approved plans only after a successfully delivered matching daily report.

The claim is committed before network IO. Ambiguous delivery is not retried
automatically; QQ has no end-to-end exactly-once guarantee. No credentials or
raw group identifiers are stored in the plan ledger.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys

if os.environ.get('FINANCE_ROOT'):
    # Existing private installations provide this explicit application path.
    APP=Path(os.environ['FINANCE_ROOT']).expanduser().resolve()
else:
    from xtrastock.config import Settings
    APP=Settings().path('paths.app')
if str(APP) not in sys.path:sys.path.insert(0,str(APP))
from scripts.analytics.value_plan import load_config,render,stamp
from scripts.analytics.run_daily_value_plan import publication_approval
from scripts.analytics.limit_execution import first_eligible_session
from psycopg.types.json import Jsonb

def prepare_payload(plans,report_date,published_at,sessions):
    first=first_eligible_session(published_at,sessions)
    result=[]
    for plan in plans:
        p=dict(plan)
        p['published_at']=str(published_at);p['first_eligible_session']=first;p['publication_status']='delivery_attempt'
        if p['state']=='eligible_to_buy' and (first is None or not p.get('entry_expires_on') or first>p['entry_expires_on']):
            p.update(state='removed',entry_limit=None,change='removed',reason=['本次推送晚于计划有效期，取消未成交买入计划'])
        result.append(p)
    text=render(result,report_date,True)
    # Split at line boundaries; every numeric instruction remains intact.
    parts=[];chunk=''
    for line in text.splitlines():
        if len(line)>3000:raise ValueError('unexpected oversized plan line')
        if len(chunk)+len(line)+1>3000:parts.append(chunk);chunk=''
        chunk+=line+'\n'
    if chunk:parts.append(chunk)
    return parts,result

def deliver_after_report(group_id,report_date,report_path,report_sent_at,send,connect,log):
    config=load_config()
    if not config.get('publish_enabled'):return {'status':'disabled'}
    now=dt.datetime.now(dt.timezone.utc)
    approved,_=publication_approval(config,now)
    if not approved:return {'status':'validation_pending'}
    if config.get('enabled_from') and report_date<config['enabled_from']:return {'status':'before_enabled_date'}
    if stamp(report_sent_at)>stamp(now):raise ValueError('report delivery timestamp is in the future')
    target=hashlib.sha256(group_id.encode()).hexdigest()
    with connect() as conn:
        conn.execute("SET LOCAL statement_timeout='10s'");conn.execute("SET LOCAL lock_timeout='2s'")
        row=conn.execute('''SELECT p.*,r.output_path,r.status report_status FROM quant.daily_plan_runs p
           JOIN agent.report_runs r ON r.report_run_id=p.report_run_id
           WHERE p.report_date=%s AND p.mode='approved' ORDER BY p.created_at DESC LIMIT 1''',(report_date,)).fetchone()
        if not row:return {'status':'waiting_for_plan'}
        if row['report_status']!='published' or Path(row['output_path']).resolve()!=Path(report_path).resolve():
            return {'status':'report_mismatch'}
        if row['validation'].get('checks') is None or not all(row['validation']['checks'].values()):
            raise ValueError('stored plan validation is incomplete')
        # Compare the complete runtime config, including activation, to the
        # frozen run; toggling config cannot retroactively publish a shadow run.
        from scripts.analytics.value_plan import digest
        if row['config_digest']!=digest(config):return {'status':'configuration_changed'}
        plans=[r['payload'] for r in conn.execute('SELECT payload FROM quant.daily_plan_revisions WHERE run_id=%s ORDER BY stock_code',(row['run_id'],))]
        sessions=[str(r['trade_date']) for r in conn.execute('''SELECT trade_date FROM market.trading_calendar WHERE exchange='SSE'
          AND is_verified AND session_status='open' AND trade_date BETWEEN %s::date AND %s::date+30 ORDER BY trade_date''',(report_date,report_date))]
        parts,delivered=prepare_payload(plans,report_date,now,sessions)
        for i,text in enumerate(parts):
            conn.execute('''INSERT INTO quant.daily_plan_deliveries(run_id,target_key,part,status,payload)
              VALUES(%s,%s,%s,'pending',%s) ON CONFLICT DO NOTHING''',
              (row['run_id'],target,i,Jsonb({'text':text,'publication_attempt_at':now.isoformat(),
                 'report_delivered_at':str(report_sent_at),'first_eligible_session':first_eligible_session(now,sessions)})))
    sent=0
    for i in range(len(parts)):
        with connect() as conn:
            claimed=conn.execute('''UPDATE quant.daily_plan_deliveries SET status='sending',updated_at=now()
                WHERE run_id=%s AND target_key=%s AND part=%s AND status='pending' RETURNING payload''',
                (row['run_id'],target,i)).fetchone()
            if not claimed:
                status=conn.execute('SELECT status FROM quant.daily_plan_deliveries WHERE run_id=%s AND target_key=%s AND part=%s',
                    (row['run_id'],target,i)).fetchone()
                if not status or status['status']!='sent':return {'status':'delivery_requires_review','sent':sent}
                continue
        try:ok=bool(send(group_id,[claimed['payload']['text']]))
        except Exception as exc:
            ok=False;log('value_plan_delivery_error',{'error_type':type(exc).__name__})
        with connect() as conn:
            conn.execute('UPDATE quant.daily_plan_deliveries SET status=%s,updated_at=now() WHERE run_id=%s AND target_key=%s AND part=%s',
                ('sent' if ok else 'uncertain',row['run_id'],target,i))
        if not ok:return {'status':'uncertain','sent':sent}
        sent+=1
    # Only a complete delivery creates a publication event. Network completion,
    # not the time before send(), is the lower bound for simulated execution.
    completed=dt.datetime.now(dt.timezone.utc)
    with connect() as conn:
        for plan in delivered:
            event_id=hashlib.sha256(f"{row['run_id']}:{target}:{plan['plan_id']}:published".encode()).hexdigest()
            conn.execute('''INSERT INTO quant.daily_plan_events(event_id,plan_id,event_type,event_at,payload)
               VALUES(%s,%s,'review',%s,%s) ON CONFLICT DO NOTHING''',
               (event_id,plan['plan_id'],completed,Jsonb({'event':'plan_published',
                 'publication_completed_at':completed.isoformat(),'first_eligible_session':first_eligible_session(completed,sessions)})))
    return {'status':'sent','parts':sent}
