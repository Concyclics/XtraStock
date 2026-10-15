#!/usr/bin/env python3
"""Plan or install repository-owned user units; activation is explicit."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

APP = Path(__file__).resolve().parents[2]
ROOT = APP.parent
DUPLICATE_TIMERS = (
    'finance-news-daily.timer', 'finance-ai-hardware-agent.timer',
    'finance-market-close.timer', 'finance-reports-daily.timer',
    'finance-analytics-core.timer', 'finance-knowledge-graph.timer',
    'finance-factor-daily.timer', 'finance-nightly-verify.timer',
    'finance-analytics-daily.timer', 'finance-market-midday.timer', 'finance-retry-plan.timer',
)
ENABLE = (
    'finance-pipeline-controller.service', 'finance-ocr-worker.service',
    'finance-document-embedding.timer', 'finance-document-index.timer',
    'finance-cninfo-bodies.timer', 'finance-research-registry.timer',
    'finance-agent-quality.timer', 'finance-meili-recent.timer',
    'finance-db-maintenance.timer', 'finance-factor-research.timer', 'finance-status-report.timer',
)
REQUIRED_RELATIONS = (
    'ops.data_asset_registry', 'research.experiment_artifacts', 'research.artifact_records',
    'research.reports_resolved', 'research.report_forecasts_usable', 'ops.source_import_versions',
    'ops.dataset_watermarks', 'ops.document_body_recovery', 'ops.document_embedding_model_state',
    'factor.valid_daily_scores_v1', 'kg.relation_versions', 'analytics.research_snapshot_runs',
    'analytics.forecast_models', 'quant.repair_queue',
)
REQUIRED_FUNCTIONS = ('ops.enqueue_research_identity_search()',)


def available_units():
    return sorted(p.name for p in (APP / 'systemd').iterdir()
                  if p.is_file() and p.suffix in {'.service', '.timer'}
                  and p.name.startswith(('finance-', 'finagent-', 'mmclaw-')))


def deployment_plan(names, target):
    known = set(available_units())
    if set(names) - known:
        raise ValueError('Unknown units: ' + ', '.join(sorted(set(names) - known)))
    plan = []
    for name in names:
        sources=[APP/'systemd'/name,*sorted((APP/'systemd'/(name+'.d')).glob('*.conf'))]
        for source in sources:
            from xtrastock.config import Settings
            from xtrastock.deployment import render
            content = render(source.read_text(), Settings()).encode()
            relative=source.relative_to(APP/'systemd')
            destination = target / relative
            plan.append({'unit':name, 'relative_path':str(relative), 'source':str(source), 'destination':str(destination),
                         'sha256':hashlib.sha256(content).hexdigest(), 'rendered_content':content.decode(),
                         'changed':not destination.is_file() or destination.read_bytes()!=content})
    return plan


def preflight():
    from scripts.analytics.common import connect
    with connect(autocommit=False) as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='5s'")
        missing = [name for name in REQUIRED_RELATIONS
                   if conn.execute('SELECT to_regclass(%s) AS present',(name,)).fetchone()['present'] is None]
        missing += [name for name in REQUIRED_FUNCTIONS
                    if conn.execute('SELECT to_regprocedure(%s) AS present',(name,)).fetchone()['present'] is None]
    if missing:
        raise RuntimeError('Required migrations missing: '+', '.join(missing))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--activate',action='store_true',help='Enable scheduled owners; never enable QQ/WeChat or history rebuilds.')
    parser.add_argument('--units',nargs='+',help='Limit files to install; default is all FinAgent units.')
    parser.add_argument('--restart',nargs='*',default=[],help='Restart only explicitly named installed units.')
    parser.add_argument('--audit-dir',type=Path)
    args = parser.parse_args()
    names = args.units or available_units()
    if set(args.restart)-set(names):parser.error('--restart units must be included in --units')
    target = Path.home()/'.config/systemd/user'
    plan = deployment_plan(names,target)
    enable = [n for n in ENABLE if n in names] if args.activate else []
    # The catch-up owner stops the periodic embedding timer until it finishes.
    catchup = subprocess.run(['systemctl','--user','is-active','finance-embedding-catchup.service'],
                             capture_output=True,text=True,check=False)
    if catchup.stdout.strip() in {'active','activating','deactivating'}:
        enable = [n for n in enable if n!='finance-document-embedding.timer']
    summary = {'install':plan, 'enable':enable,'restart':args.restart,
               'disable_duplicate_timers':list(DUPLICATE_TIMERS) if args.activate else [],
               'apply':args.apply,'default_behavior':'Plan only; --apply installs without activation or restart.'}
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    if not args.apply:return 0
    preflight()
    audit = args.audit_dir or __import__('xtrastock.config',fromlist=['Settings']).Settings().path('paths.state')/'migration'/('deploy-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    audit.mkdir(mode=0o700,parents=True,exist_ok=False)
    states = {}
    for name in dict.fromkeys([*names,*DUPLICATE_TIMERS]):
        result = subprocess.run(['systemctl','--user','is-enabled',name],capture_output=True,text=True)
        states[name] = result.stdout.strip()
        if (target/name).is_file():shutil.copy2(target/name,audit/name)
    (audit/'plan.json').write_text(json.dumps(summary,indent=2))
    (audit/'enable-state.json').write_text(json.dumps(states,indent=2))
    target.mkdir(parents=True,exist_ok=True)
    for item in plan:
        if item['changed']:
            destination=Path(item['destination'])
            if destination.is_file():
                backup=audit/item['relative_path'];backup.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(destination,backup)
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_text(item['rendered_content'])
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    if args.activate:
        subprocess.run(['systemctl','--user','disable','--now',*DUPLICATE_TIMERS],check=True)
        if enable:subprocess.run(['systemctl','--user','enable','--now',*enable],check=True)
    if args.restart:subprocess.run(['systemctl','--user','restart',*args.restart],check=True)
    print('Unit audit saved: '+str(audit))
    return 0


if __name__ == '__main__':
    import sys
    sys.path.insert(0,str(APP))
    raise SystemExit(main())
