#!/usr/bin/env python3
"""Bounded review repair with exact before/after records and no evidence deletion."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import subprocess
import shutil
from collections import Counter
from common import connect,repo_root
from graph_revision_policy import GRAPH_WRITE_LOCK,RECONCILE_EXPOSURES_SQL

SNAPSHOT_SQL="""SELECT v.relation_version_id,v.source_system,v.status,to_jsonb(v) AS record
FROM kg.relation_versions v WHERE v.source_system IN
 ('company_concept_exposures','kg.company_concept_exposures') ORDER BY v.relation_version_id"""

def save(path:Path,data:object):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():raise RuntimeError('refusing to overwrite audit file '+str(path))
    with path.open('x',encoding='utf-8') as f:
        os.chmod(path,0o600);json.dump(data,f,ensure_ascii=False,indent=2,default=str);f.write('\n');f.flush();os.fsync(f.fileno())

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--apply',action='store_true')
    p.add_argument('--apply-migration',action='store_true')
    p.add_argument('--expected-legacy-count',type=int,default=1661)
    p.add_argument('--wait-seconds',type=int,default=0)
    p.add_argument('--deploy-candidates',type=Path)
    p.add_argument('--rebuild-as-of')
    p.add_argument('--knowledge-mode',choices=('public','system'),default='public')
    args=p.parse_args()
    if args.apply_migration and not args.apply:p.error('--apply-migration requires --apply')
    if (args.deploy_candidates or args.rebuild_as_of) and not args.apply:p.error('deployment/rebuild requires --apply')
    started=dt.datetime.now(dt.timezone.utc).isoformat()
    if args.deploy_candidates:
        for name in ['build_temporal_knowledge_graph.py','build_temporal_graph_features_daily.py']:
            candidate=args.deploy_candidates/name
            original=args.deploy_candidates.parent/'original/scripts/analytics'/name
            installed=repo_root()/'scripts/analytics'/name
            if not candidate.is_file() or not original.is_file():raise RuntimeError('missing reviewed candidate or original '+name)
            if hashlib.sha256(original.read_bytes()).digest()!=hashlib.sha256(installed.read_bytes()).digest():
                raise RuntimeError('installed script changed since review: '+name)
            compile(candidate.read_text(),str(candidate),'exec')
    if args.rebuild_as_of:dt.date.fromisoformat(args.rebuild_as_of)
    with connect(admin=args.apply,autocommit=True,attempts=1) as conn:
        # This is the exact existing pipeline lock; never interrupt an existing task.
        deadline=time.monotonic()+max(0,args.wait_seconds)
        while not conn.execute("SELECT pg_try_advisory_lock(hashtextextended('finance-pipeline:knowledge_graph',0)) acquired").fetchone()['acquired']:
            if time.monotonic()>=deadline:
                print('BUSY: existing knowledge_graph pipeline is still running; no changes made',flush=True);return 75
            print('WAIT: existing knowledge_graph pipeline owns its lock; no changes made',flush=True)
            time.sleep(min(30,max(0,deadline-time.monotonic())))
        try:
            if args.apply_migration:
                migration=repo_root()/'docker/postgres/migrate_graph_revision_history_v5.sql'
                conn.execute(migration.read_text())
                save(args.output/'migration.json',{'at':started,'file':str(migration),'sha256':hashlib.sha256(migration.read_bytes()).hexdigest(),'status':'applied'})
            with conn.transaction():
                conn.execute("SET LOCAL lock_timeout='5s'")
                conn.execute("SET LOCAL statement_timeout='60s'")
                if not conn.execute('SELECT pg_try_advisory_xact_lock(%s) acquired',(GRAPH_WRITE_LOCK,)).fetchone()['acquired']:
                    raise RuntimeError('another graph writer holds the graph transaction lock')
                snapshot_sql=SNAPSHOT_SQL+(" FOR UPDATE OF v" if args.apply else "")
                before=conn.execute(snapshot_sql).fetchall()
                legacy=sum(r['source_system']=='company_concept_exposures' for r in before)
                if legacy!=args.expected_legacy_count:raise RuntimeError(f'legacy scope changed: expected {args.expected_legacy_count}, got {legacy}')
                save(args.output/'before.json',{'at':started,'query':snapshot_sql,'records':before})
                if not args.apply:
                    print(json.dumps({'status':'dry_run','legacy_rows':legacy,'total_rows':len(before)}));return 0
                if conn.execute("SELECT to_regclass('kg.version_revision_history') relation").fetchone()['relation'] is None:
                    raise RuntimeError('revision history migration is required before repair')
                ids=[r['relation_version_id'] for r in before]
                updated=conn.execute(RECONCILE_EXPOSURES_SQL).rowcount
                after=conn.execute(SNAPSHOT_SQL).fetchall()
                unsupported=conn.execute("""SELECT count(*) n FROM kg.relation_versions WHERE source_system IN
                   ('company_concept_exposures','kg.company_concept_exposures') AND status='active'
                   AND coalesce(attributes->>'superseded_by_canonical','false')<>'true'
                   AND attributes->'attribution_check'->>'supported' IS DISTINCT FROM 'true'""").fetchone()['n']
                if unsupported:raise RuntimeError(f'{unsupported} unsupported formal exposure versions remain; rolling back')
                history_count=conn.execute("SELECT count(*) n FROM kg.version_revision_history WHERE version_kind='relation' AND source_version_id=ANY(%s)",(ids,)).fetchone()['n']
                save(args.output/'after-transaction.json',{'at':dt.datetime.now(dt.timezone.utc).isoformat(),'query':SNAPSHOT_SQL,'records':after,'updated_rows':updated,'history_rows':history_count})
            summary={'status':'committed','at':dt.datetime.now(dt.timezone.utc).isoformat(),'updated_rows':updated,'history_rows':history_count,'legacy_rows':legacy,
               'after_counts':dict(Counter(r['source_system']+':'+r['status'] for r in after)),
               'unsupported_formal_exposures':unsupported,'before_file':str(args.output/'before.json'),
               'after_file':str(args.output/'after-transaction.json'),
               'rollback':'Restore status/review_status/attributes from before.json only after verifying current values still match after-transaction.json. The revision trigger will preserve this repair as a superseded revision; never delete the history ledger.'}
            save(args.output/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
            deployed=[]
            if args.deploy_candidates:
                for name in ['build_temporal_knowledge_graph.py','build_temporal_graph_features_daily.py']:
                    candidate=args.deploy_candidates/name;target=repo_root()/'scripts/analytics'/name
                    staging=target.with_name('.'+name+'.graph-upgrade-'+str(os.getpid()))
                    shutil.copy2(candidate,staging);os.replace(staging,target)
                    deployed.append({'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
                save(args.output/'deployment.json',{'at':dt.datetime.now(dt.timezone.utc).isoformat(),'files':deployed})
            if args.rebuild_as_of:
                query="SELECT to_jsonb(g) record FROM analytics.graph_feature_daily g WHERE dt=%s ORDER BY asset_id"
                save(args.output/'features-before.json',{'date':args.rebuild_as_of,'query':query,'records':conn.execute(query,(args.rebuild_as_of,)).fetchall()})
                command=[str(repo_root()/sys.executable),str(repo_root()/'scripts/analytics/build_temporal_graph_features_daily.py'),
                    '--as-of',args.rebuild_as_of,'--knowledge-mode',args.knowledge_mode,'--statement-timeout-seconds','300']
                with (args.output/'features-rebuild.log').open('x') as log:
                    result=subprocess.run(command,cwd=repo_root(),stdout=log,stderr=subprocess.STDOUT,timeout=330)
                if result.returncode:raise RuntimeError('single-date graph rebuild failed with exit '+str(result.returncode))
                save(args.output/'features-after.json',{'date':args.rebuild_as_of,'query':query,'records':conn.execute(query,(args.rebuild_as_of,)).fetchall()})
            save(args.output/'finalizer.json',{'status':'complete','at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'deployment_files':deployed,'rebuilt_date':args.rebuild_as_of,'knowledge_mode':args.knowledge_mode,
                'published_research_snapshots_modified':False})
            print('FINALIZER complete',flush=True)
        finally:conn.execute("SELECT pg_advisory_unlock(hashtextextended('finance-pipeline:knowledge_graph',0))")
    return 0
if __name__=='__main__':raise SystemExit(main())
