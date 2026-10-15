#!/usr/bin/env python3
"""Read-only repository/deployment/database contract audit; never starts jobs."""
from __future__ import annotations
import argparse
import ast
import datetime as dt
import json
from pathlib import Path
import sys

APP=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(APP))
from scripts.analytics.data_asset_contracts import DATASETS
from scripts.ops.deploy_finagent import available_units,deployment_plan,REQUIRED_RELATIONS,REQUIRED_FUNCTIONS


def audit(database=False):
    errors=[];warnings=[]
    components=json.loads((APP/'config/repository_components.json').read_text())['components']
    for item in components:
        if not (APP/item['path']).exists():errors.append('Missing component: '+item['path'])
    code_files=[]
    for directory in ('scripts','tests','runtime','MMClaw/mmclaw','MMClaw/tests','Spider/eastmoney/eastmoney','Spider/eastmoney/scripts'):
        for path in sorted((APP/directory).rglob('*.py')):
            if any(part in {'.venv','node_modules','__pycache__'} for part in path.parts):continue
            try:ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
            except (SyntaxError,UnicodeError) as exc:errors.append(str(path.relative_to(APP))+': '+type(exc).__name__)
            code_files.append(str(path.relative_to(APP)))
    definitions={row[0]:row for row in DATASETS}
    for key,*_,deps in DATASETS:
        if set(deps)-definitions.keys():errors.append('Unknown dependencies: '+key)
    units=deployment_plan(available_units(),Path.home()/'.config/systemd/user')
    for row in units:
        if row['changed']:warnings.append('Installed unit differs or is absent: '+row['unit'])
    database_status=None
    if database:
        from scripts.analytics.common import connect
        with connect(autocommit=False,attempts=1) as conn:
            conn.execute('SET TRANSACTION READ ONLY');conn.execute("SET LOCAL statement_timeout='8s'")
            relations=set(REQUIRED_RELATIONS)
            for _,_,kind,locator,*_ in DATASETS:
                if kind=='sql':relations.update(locator.split(';'))
            missing=[name for name in sorted(relations) if conn.execute('SELECT to_regclass(%s) r',(name,)).fetchone()['r'] is None]
            registered={row['dataset_key'] for row in conn.execute('SELECT dataset_key FROM ops.data_asset_registry').fetchall()}
            missing_definitions=sorted(definitions.keys()-registered)
            missing_functions=[name for name in REQUIRED_FUNCTIONS if conn.execute('SELECT to_regprocedure(%s) r',(name,)).fetchone()['r'] is None]
            errors.extend('Missing relation: '+name for name in missing)
            errors.extend('Unregistered dataset: '+name for name in missing_definitions)
            errors.extend('Missing function: '+name for name in missing_functions)
            database_status={'checked_relations':len(relations),'registered_datasets':len(registered),
                             'missing_relations':missing,'missing_definitions':missing_definitions,
                             'checked_functions':len(REQUIRED_FUNCTIONS),'missing_functions':missing_functions,'read_only':True}
    return {'at':dt.datetime.now(dt.timezone.utc).isoformat(),'passed':not errors,
            'components':len(components),'python_files_checked':len(code_files),'dataset_definitions':len(DATASETS),
            'units_checked':len(units),'database':database_status,'errors':errors,'warnings':warnings,
            'scope':'Code syntax, maintained entrypoints, deployment drift and SQL object existence; not content-completeness certification.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args();result=audit(args.database)
    body=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(body+'\n')
    print(body)
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
