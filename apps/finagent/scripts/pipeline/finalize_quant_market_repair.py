#!/usr/bin/env python3
"""Explicit, checkpointed recent-data rebuild after the market and graph repairs.

Default is plan-only. No LLM, factor promotion, all-history replay, order broker,
background restart, or quality-gate bypass. Nonzero child exits stop the chain;
completed writes and each attempt's log remain available for a reviewed resume.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.pipeline.common import connect, pipeline_lock

VERSION = 'market-repair-finalizer-v1'
TZ = ZoneInfo('Asia/Shanghai')
PIPELINE_LOCKS = ('market_close', 'analytics_core', 'knowledge_graph', 'factor_daily')
TERMINAL = {'done', 'exhausted'}
SCORE_COLUMNS = {'input_contract_version', 'quality_status', 'source_feature_updated_at',
                 'source_factor_version', 'source_decision_cutoff', 'source_cohort_hash', 'invalid_reason'}


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def queue_evidence(state_dir: Path):
    path = state_dir / 'queue.sqlite3'
    if not path.exists():
        return {'ready': False, 'reason': 'queue missing'}
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        groups = [dict(r) for r in conn.execute(
            'SELECT status,count(*) jobs,sum(received) received,sum(written) written,'
            'sum(attempts) attempts,max(updated_at) last_update FROM jobs GROUP BY status ORDER BY status')]
        exceptions = [dict(r) for r in conn.execute(
            "SELECT secid,status,attempts,last_error FROM jobs WHERE status='exhausted' ORDER BY secid")]
    ready = bool(groups) and all(r['status'] in TERMINAL for r in groups)
    fingerprint = hashlib.sha256(json.dumps(groups, sort_keys=True).encode()).hexdigest()
    return {'ready': ready, 'all_successful': bool(groups) and all(r['status'] == 'done' for r in groups),
            'groups': groups, 'unresolved': exceptions, 'fingerprint': fingerprint}


def graph_evidence(path: Path):
    if not path.exists():
        return {'ready': False, 'reason': 'completion file missing'}
    payload = json.loads(path.read_text())
    if payload.get('status') in {'failed', 'error', 'stopped'}:
        raise RuntimeError('graph finalizer failed: see ' + str(path))
    ready = (payload.get('status') == 'complete' and bool(payload.get('at'))
             and bool(payload.get('deployment_files'))
             and payload.get('rebuilt_date') == '2026-10-12'
             and payload.get('knowledge_mode') == 'public'
             and payload.get('published_research_snapshots_modified') is False)
    return {'ready': ready, 'payload': payload,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def plan_hash(tasks):
    return hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest()


def dependency_hash(market, graph):
    if not market.get('ready') or not graph.get('ready'):
        raise RuntimeError('reuse requires terminal market and completed graph evidence')
    return hashlib.sha256((market['fingerprint']+graph['sha256']).encode()).hexdigest()


def validate_reuse_checkpoint(path, tasks, since, until, as_of):
    """Validate receipts without changing the source or trusting completed alone."""
    path=Path(path).resolve();raw=path.read_bytes();source=json.loads(raw)
    if source.get('status') not in {'stopped','failed'} or not source.get('finished_at'):
        raise RuntimeError('reuse source must be terminal stopped/failed, not a running or completed chain')
    if source.get('version')!=VERSION or source.get('range')!=[since,until] or source.get('as_of')!=as_of:
        raise RuntimeError('reuse version/range/as-of mismatch')
    old_steps=source.get('steps') or []
    if source.get('plan_sha256')!=plan_hash(old_steps):
        raise RuntimeError('reuse source plan fingerprint is invalid')
    old_names=[s['name'] for s in old_steps];names=[s['name'] for s in tasks]
    if old_names!=names or len(set(names))!=len(names):
        raise RuntimeError('reuse requires identical ordered stage names')
    completed=source.get('completed')
    if not isinstance(completed,list) or completed!=names[:len(completed)]:
        raise RuntimeError('reuse completed stages must be an exact plan prefix')
    expected=dependency_hash(source.get('market') or {},source.get('graph') or {})
    if source.get('dependency_sha256')!=expected:
        raise RuntimeError('reuse source dependency fingerprint is missing or inconsistent')
    receipt_candidates=[*source.get('inherited_success_receipts',[]),*source.get('attempts',[])]
    receipts=[]
    source_sha=hashlib.sha256(raw).hexdigest()
    for old,new in zip(old_steps[:len(completed)],tasks):
        name=old['name']
        if old.get('command')!=new.get('command') or old.get('script_sha256')!=new.get('script_sha256'):
            raise RuntimeError('completed stage command/script changed: '+name)
        candidates=[a for a in receipt_candidates if a.get('name')==name]
        last=candidates[-1] if candidates else {}
        if type(last.get('returncode')) is not int or last.get('returncode')!=0 or not last.get('finished_at'):
            raise RuntimeError('completed stage lacks a last successful attempt: '+name)
        if last.get('command')!=new['command']:
            raise RuntimeError('successful receipt command mismatch: '+name)
        if last.get('script_sha256',old['script_sha256'])!=new['script_sha256']:
            raise RuntimeError('successful receipt script fingerprint mismatch: '+name)
        receipts.append({**last,'script_sha256':old['script_sha256'],
            'script_sha256_basis':last.get('script_sha256_basis') or ('attempt' if 'script_sha256' in last else 'source_plan_v1_runtime_checked'),
            'inherited_from':str(path),'inherited_checkpoint_sha256':source_sha})
    audit={'checkpoint':str(path),'checkpoint_sha256':source_sha,'source_status':source['status'],
        'source_plan_sha256':source['plan_sha256'],'source_version':source['version'],
        'source_finished_at':source['finished_at'],'source_error':source.get('error'),
        'source_attempts':source.get('attempts',[]),
        'source_inherited_success_receipts':source.get('inherited_success_receipts',[]),
        'dependency_sha256':expected,'market_fingerprint':source['market']['fingerprint'],
        'graph_sha256':source['graph']['sha256'],'validated_at':utcnow()}
    return {'audit':audit,'completed':list(completed),'receipts':receipts,'source_bytes':raw}


@contextmanager
def reusable_source(path, state_dir, tasks, since, until, as_of):
    if Path(state_dir).exists():
        raise RuntimeError('--reuse-completed-from requires a new, nonexistent state directory')
    path=Path(path).resolve()
    # Existing read-only handle; do not create/change the old lock or checkpoint.
    with path.with_name('finalizer.lock').open('rb') as source_lock:
        try:
            fcntl.flock(source_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('reuse source finalizer is still running/locked') from exc
        yield validate_reuse_checkpoint(path,tasks,since,until,as_of)


def verify_reuse_dependencies(reuse, market, graph):
    audit=reuse['audit']
    if (dependency_hash(market,graph)!=audit['dependency_sha256'] or
        market['fingerprint']!=audit['market_fingerprint'] or graph['sha256']!=audit['graph_sha256']):
        raise RuntimeError('reuse dependency evidence changed; completed stages cannot be inherited')
    if hashlib.sha256(Path(audit['checkpoint']).read_bytes()).hexdigest()!=audit['checkpoint_sha256']:
        raise RuntimeError('reuse source checkpoint changed after review')


def plan(since: str, until: str, as_of: str):
    py = str(ROOT / sys.executable)
    def task(name, script, args):
        path = ROOT / 'scripts/analytics' / script
        return {'name': name, 'command': [py, str(path), *args],
                'script_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    date_range = ['--since', since, '--until', until]
    today_range = ['--since', as_of, '--until', as_of]
    return [
        task('canonical', 'sync_canonical_daily_bars_v2.py', [*date_range, '--asset-types', 'stock,index']),
        task('adjustment', 'build_adjustment_factors_v3.py', date_range),
        task('technical', 'build_technical_signals.py', [*date_range, '--asset-types', 'stock']),
        task('system_graph', 'build_temporal_graph_features_daily.py', ['--as-of', as_of, '--knowledge-mode', 'system']),
        task('features', 'build_factor_features.py', [*today_range, '--knowledge-mode', 'system', '--skip-labels']),
        task('scores', 'build_factor_scores.py', [*today_range, '--status', 'shadow,production', '--min-cohort-size', '2']),
        task('health', 'refresh_factor_health.py', today_range),
        task('snapshot', 'build_research_snapshot.py', ['--date', as_of]),
        task('readiness', 'run_quant_paper_daily.py', ['--date', as_of, '--readiness-only']),
    ]


def schema_preflight():
    with connect() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='25s'")
        found = {r['column_name'] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='factor' AND table_name='daily_scores'")}
        missing = SCORE_COLUMNS - found
        if missing:
            raise RuntimeError('score contract schema not installed: ' + ','.join(sorted(missing)))
        required = ('market.asset_identity_aliases', 'factor.daily_score_revisions',
                    'kg.version_history_contract', 'quant.readiness_runs')
        for table in required:
            if not conn.execute('SELECT to_regclass(%s) AS relation', (table,)).fetchone()['relation']:
                raise RuntimeError('missing required schema: ' + table)
        contract = conn.execute(
            "SELECT installed_at FROM kg.version_history_contract WHERE contract_key='revision-v5'").fetchone()
        if not contract:
            raise RuntimeError('revision-v5 graph contract missing')
    return {'score_columns': sorted(found), 'graph_contract': dict(contract)}


def acquire_pipeline_locks(deadline: float):
    while True:
        stack = ExitStack()
        try:
            for name in PIPELINE_LOCKS:
                stack.enter_context(pipeline_lock(name))
            return stack
        except RuntimeError as exc:
            stack.close()
            if 'pipeline already running:' not in str(exc):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError('pipeline-lock deadline: ' + str(exc)) from exc
            print(str(exc) + '; waiting without stopping other jobs', flush=True)
            time.sleep(min(30, max(0, deadline - time.monotonic())))
        except BaseException:
            stack.close()
            raise


def readiness_from_log(path: Path):
    for line in reversed(path.read_text(errors='replace').splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        status = value.get('status') or value.get('readiness_status')
        nested = value.get('readiness')
        if isinstance(nested, dict):
            status = nested.get('status', status)
        elif nested in {'ready', 'blocked'}:
            status = nested
        if status in {'ready', 'blocked'}:
            return status, value
    raise RuntimeError('readiness child exited successfully but no ready/blocked result was found')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', default='2026-08-01')
    parser.add_argument('--until', default='2026-10-13')
    parser.add_argument('--as-of', default='2026-10-13')
    parser.add_argument('--market-state-dir', type=Path, default=ROOT/'logs/market_gap_repair_20261013')
    parser.add_argument('--graph-completion', type=Path, default=ROOT.parent/'migration/quant-framework-upgrade-20261013T140808Z/graph-live/finalizer.json')
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--execute', action='store_true', help='Required to wait, lock and run mutations; otherwise print plan only')
    parser.add_argument('--resume', action='store_true', help='Explicit retry of a stopped chain with unchanged plan and dependency evidence')
    parser.add_argument('--reuse-completed-from', type=Path,
        help='Audit and inherit only unchanged successful prefix stages into a new state directory; source must be stopped/failed')
    parser.add_argument('--wait-seconds', type=int, default=21600)
    parser.add_argument('--step-timeout-seconds', type=int, default=3600)
    args = parser.parse_args()
    if args.resume and args.reuse_completed_from:
        parser.error('--resume and --reuse-completed-from are mutually exclusive')
    since, until, as_of = map(dt.date.fromisoformat, (args.since, args.until, args.as_of))
    if since > until or (until-since).days > 100 or as_of != until or until > dt.datetime.now(TZ).date():
        parser.error('require since<=until, window<=101 calendar days, as-of=until, and no future date')
    tasks = plan(args.since, args.until, args.as_of)
    signature = plan_hash(tasks)
    guard=reusable_source(args.reuse_completed_from,args.state_dir,tasks,args.since,args.until,args.as_of) if args.reuse_completed_from else nullcontext(None)
    with guard as reuse:
        if not args.execute:
            if reuse:
                verify_reuse_dependencies(reuse,queue_evidence(args.market_state_dir),graph_evidence(args.graph_completion))
            print(json.dumps({'mode': 'plan_only', 'version': VERSION, 'steps': tasks,
                              'locks': PIPELINE_LOCKS, 'source_writes': False,
                              'market_dependency': str(args.market_state_dir), 'graph_dependency': str(args.graph_completion),
                              'reuse_completed_from':reuse['audit'] if reuse else None,
                              'inherited_completed':reuse['completed'] if reuse else [],
                              'remaining_steps':[s['name'] for s in tasks if not reuse or s['name'] not in reuse['completed']]}, indent=2))
            return 0
        return run_chain(args,tasks,signature,reuse)


def run_chain(args,tasks,signature,reuse=None):
    args.state_dir.mkdir(parents=True, exist_ok=not bool(reuse))
    own_lock = (args.state_dir/'finalizer.lock').open('a')
    fcntl.flock(own_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state_path = args.state_dir/'checkpoint.json'
    if state_path.exists():
        if not args.resume:
            raise RuntimeError('state exists; inspect checkpoint and explicitly choose --resume or a new state directory')
        state = json.loads(state_path.read_text())
        if state.get('plan_sha256') != signature:
            raise RuntimeError('plan/script changed; old successful checkpoints cannot be reused: choose a new state directory after review')
        if state.get('status') in {'complete_ready', 'completed_data_rebuild_blocked_research'}:
            print(json.dumps(state, ensure_ascii=False));return 0
    else:
        state = {'version': VERSION, 'created_at': utcnow(), 'plan_sha256': signature,
                 'range': [args.since,args.until], 'as_of':args.as_of,'steps':tasks,'attempts':[], 'completed':[]}
        if reuse:
            source_copy=args.state_dir/'inherited-source-checkpoint.json'
            with source_copy.open('xb') as output:
                output.write(reuse['source_bytes']);output.flush();os.fsync(output.fileno())
            state.update(completed=reuse['completed'],inherited_success_receipts=reuse['receipts'],
                reuse_completed_from={**reuse['audit'],'source_copy':str(source_copy)},
                dependency_sha256=reuse['audit']['dependency_sha256'])
    state['status'] = 'waiting_dependencies';write_json(state_path, state)
    deadline = time.monotonic()+max(1,args.wait_seconds)
    child = None
    worker_lock = None
    def interrupt(signum, _frame):
        raise InterruptedError('finalizer received signal ' + str(signum))
    signal.signal(signal.SIGINT, interrupt);signal.signal(signal.SIGTERM, interrupt)
    try:
        while True:
            market = queue_evidence(args.market_state_dir);graph = graph_evidence(args.graph_completion)
            state.update(market=market, graph=graph, checked_at=utcnow());write_json(state_path, state)
            if market['ready'] and graph['ready']:
                candidate = (args.market_state_dir/'worker.lock').open('a')
                try:
                    fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    candidate.close()
                else:
                    worker_lock = candidate
                    market = queue_evidence(args.market_state_dir)
                    if market['ready']:
                        break
                    worker_lock.close();worker_lock=None
            if time.monotonic() >= deadline:
                raise TimeoutError('dependency deadline reached; no rebuild started')
            time.sleep(min(30,max(0,deadline-time.monotonic())))
        dependency_signature = dependency_hash(market,graph)
        if state.get('dependency_sha256') and state['dependency_sha256'] != dependency_signature:
            raise RuntimeError('repair dependency evidence changed since prior attempt; choose a reviewed new state directory')
        state.update(dependency_sha256=dependency_signature, schema=schema_preflight())
        with acquire_pipeline_locks(deadline):
            # Hold all four exact existing pipeline locks across the chain so daily
            # runs cannot mutate an upstream table between dependent stages.
            if reuse:
                # Dependencies may have changed while waiting for pipeline locks.
                verify_reuse_dependencies(reuse,queue_evidence(args.market_state_dir),graph_evidence(args.graph_completion))
                for step in tasks[:len(reuse['completed'])]:
                    if hashlib.sha256(Path(step['command'][1]).read_bytes()).hexdigest()!=step['script_sha256']:
                        raise RuntimeError('inherited stage code changed after review: '+step['name'])
            state.update(status='running', locks=list(PIPELINE_LOCKS));write_json(state_path,state)
            for step in tasks:
                if step['name'] in state['completed']:
                    continue
                if hashlib.sha256(Path(step['command'][1]).read_bytes()).hexdigest()!=step['script_sha256']:
                    raise RuntimeError('step code changed after planning: '+step['name'])
                number = sum(a['name']==step['name'] for a in state['attempts'])+1
                logfile=args.state_dir/f"{step['name']}.attempt-{number}.log"
                attempt={'name':step['name'],'attempt':number,'started_at':utcnow(),'command':step['command'],
                         'script_sha256':step['script_sha256'],'log':str(logfile)}
                state['attempts'].append(attempt);state['current_step']=step['name'];write_json(state_path,state)
                print('START '+step['name'],flush=True)
                with logfile.open('a') as output:
                    child=subprocess.Popen(step['command'],cwd=ROOT,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
                    try:
                        result=child.wait(timeout=max(30,args.step_timeout_seconds))
                    except BaseException:
                        os.killpg(child.pid,signal.SIGTERM)
                        try:child.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid,signal.SIGKILL);child.wait()
                        attempt.update(returncode=child.returncode,finished_at=utcnow());write_json(state_path,state)
                        child=None;raise
                    child=None
                attempt.update(returncode=result,finished_at=utcnow());write_json(state_path,state)
                if result:
                    raise RuntimeError(f"{step['name']} exit {result}; retained {logfile}; subsequent stages not run")
                state['completed'].append(step['name']);write_json(state_path,state)
                print('DONE '+step['name'],flush=True)
            readiness_attempt=[a for a in [*state.get('inherited_success_receipts',[]),*state['attempts']]
                               if a['name']=='readiness' and a.get('returncode')==0][-1]
            readiness_status, readiness=readiness_from_log(Path(readiness_attempt['log']))
            state.update(status='complete_ready' if readiness_status=='ready' else 'completed_data_rebuild_blocked_research',
                         readiness=readiness,finished_at=utcnow(),factor_production_approval='not_performed')
            write_json(state_path,state);print(json.dumps({'status':state['status'],'checkpoint':str(state_path)},ensure_ascii=False));return 0
    except BaseException as exc:
        state.update(status='stopped' if isinstance(exc,InterruptedError) else 'failed',error=type(exc).__name__+': '+str(exc),finished_at=utcnow())
        write_json(state_path,state);print(json.dumps({'status':state['status'],'error':state['error'],'checkpoint':str(state_path)},ensure_ascii=False),flush=True)
        return 130 if isinstance(exc,InterruptedError) else 1
    finally:
        if worker_lock is not None:worker_lock.close()


if __name__=='__main__':
    raise SystemExit(main())
