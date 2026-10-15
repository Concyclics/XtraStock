"""Register source locations and immutable, checksummed local research artifacts.

This is an evidence catalog, never model promotion. Only bounded tabular/JSON
results are imported; large arrays/raw archives stay on user-controlled disk.
"""
from __future__ import annotations
import argparse,csv,datetime as dt,hashlib,json,math,mimetypes,os,re
from pathlib import Path
from psycopg.types.json import Jsonb

ROOT=Path(__file__).resolve().parents[2]
try:
 from .data_asset_contracts import DATASETS
except ImportError:
 from data_asset_contracts import DATASETS

SKIP_DIRS={'historical-source','code-before','code_before','deps','.git','__pycache__','.venv','node_modules','bodies','pdfs'}
SUFFIXES={'.json','.jsonl','.gz','.csv','.tsv','.npz','.npy','.parquet','.md','.ipynb','.sql'}
SECRET=re.compile(r'password|secret|api.?key|credential|authorization|private.?key|access.?token',re.I)
RESULT_NAMES=re.compile(r'(?:result|metric|evaluation|summary|costed|protocol|manifest|quality|coverage|validation|contract)',re.I)

def safe_payload(value):
 if isinstance(value,dict):
  if any(SECRET.search(str(k)) for k in value):raise ValueError('sensitive-key')
  return {str(k):safe_payload(v) for k,v in value.items()}
 if isinstance(value,list):return [safe_payload(v) for v in value]
 if isinstance(value,float) and not math.isfinite(value):raise ValueError('non-finite-number')
 if isinstance(value,str) and (re.search(r'Bearer\s+\S+|sk-[A-Za-z0-9_-]{16,}|-----BEGIN .*PRIVATE KEY',value)):
  raise ValueError('sensitive-value')
 return value

def walk_artifacts(base):
 for directory,dirs,files in os.walk(base,followlinks=False):
  dirs[:]=sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(('bodies','pdfs','.')) and not (Path(directory)/d).is_symlink())
  for name in sorted(files):
   path=Path(directory)/name
   if path.suffix.lower() in SUFFIXES and not name.startswith('.') and not path.is_symlink() and not SECRET.search(name):yield path

def read_records(path):
 if not RESULT_NAMES.search(path.name) or path.stat().st_size>2_000_000:return [],'registered_file'
 try:
  if path.suffix.lower()=='.json':
   value=json.loads(path.read_text());rows=value if isinstance(value,list) else [value]
  elif path.suffix.lower() in {'.csv','.tsv'}:
   with path.open(newline='') as f:rows=list(csv.DictReader(f,delimiter='\t' if path.suffix.lower()=='.tsv' else ','))
  else:return [],'registered_file'
  if len(rows)>10000:return [],'registered_file_row_limit'
  safe_payload(rows)
  return rows,'sql_records'
 except (UnicodeError,json.JSONDecodeError,csv.Error,ValueError):return [],'registered_file_payload_not_imported'

def register_catalog(conn):
 """Update the small canonical catalog without scanning source files."""
 for key,title,kind,locator,grain,contract,deps in DATASETS:
  conn.execute('''INSERT INTO ops.data_asset_registry(dataset_key,title,storage_kind,locator,grain,usage_contract,dependencies)
    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(dataset_key) DO UPDATE SET title=excluded.title,
    storage_kind=excluded.storage_kind,locator=excluded.locator,grain=excluded.grain,usage_contract=excluded.usage_contract,
    dependencies=excluded.dependencies,updated_at=now()
    WHERE (ops.data_asset_registry.title,ops.data_asset_registry.storage_kind,ops.data_asset_registry.locator,
           ops.data_asset_registry.grain,ops.data_asset_registry.usage_contract,ops.data_asset_registry.dependencies)
      IS DISTINCT FROM (excluded.title,excluded.storage_kind,excluded.locator,excluded.grain,excluded.usage_contract,excluded.dependencies)
    ''',(key,title,kind,locator,grain,contract,deps))

def project_roots(base, project_names=None):
 if not project_names:return [base]
 roots=[]
 for name in sorted(set(project_names)):
  if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',name):raise ValueError('Invalid project name')
  path=base/name
  if not path.is_dir() or path.is_symlink():raise ValueError('Project must be an existing local directory')
  roots.append(path)
 return roots

def register(conn,base,project_names=None):
 roots=project_roots(base,project_names)
 counts={'registered':0,'unchanged':0,'changing':0,'payload_rows':0,'errors':[]}
 register_catalog(conn)
 # Raw historical downloads are registered as source datasets; their parent
 # manifests/worklists retain per-document provenance. Do not rehash tens of
 # thousands of concurrently downloading response files as experiment results.
 for project in sorted(base.iterdir()):
  if not project.is_dir() or project.is_symlink():continue
  if project_names and project.name not in project_names:continue
  source=project/'historical-source'
  if source.is_dir():
   conn.execute('''INSERT INTO ops.data_asset_registry(dataset_key,title,storage_kind,locator,grain,usage_contract)
     VALUES(%s,%s,'files',%s,'source document ID','原始下载目录；按同目录manifest/worklist定位；活动采集不代表完整。')
     ON CONFLICT(dataset_key) DO UPDATE SET updated_at=now()''',
     ('research.raw.'+project.name,project.name+'原始历史来源',source.relative_to(base.parent).as_posix()))
 for path in (path for root in roots for path in walk_artifacts(root)):
  relative=path.relative_to(base.parent).as_posix();project=path.relative_to(base).parts[0]
  try:
   before=path.stat()
   old=conn.execute('SELECT size_bytes,source_mtime_ns FROM research.latest_experiment_artifacts WHERE relative_path=%s',(relative,)).fetchone()
   if old and old['size_bytes']==before.st_size and old['source_mtime_ns']==before.st_mtime_ns:
    counts['unchanged']+=1;continue
   with path.open('rb') as f:digest=hashlib.file_digest(f,'sha256').hexdigest()
   records,payload_status=read_records(path);after=path.stat()
   if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
    counts['changing']+=1;continue
   with conn.transaction():
    row=conn.execute('''INSERT INTO research.experiment_artifacts(project_key,relative_path,content_sha256,size_bytes,
      source_mtime_ns,media_type,artifact_kind,payload_status,record_count) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
      ON CONFLICT(relative_path,content_sha256) DO UPDATE SET verified_at=now(),source_mtime_ns=excluded.source_mtime_ns
      RETURNING artifact_id''',(project,relative,digest,after.st_size,after.st_mtime_ns,
      mimetypes.guess_type(path.name)[0] or 'application/octet-stream',path.suffix.lstrip('.'),payload_status,len(records))).fetchone()
    for i,payload in enumerate(records):
     conn.execute('INSERT INTO research.artifact_records VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(row['artifact_id'],i,Jsonb(payload)))
   counts['registered']+=1;counts['payload_rows']+=len(records)
   if counts['registered']%50==0:
    conn.commit();print(json.dumps({k:v for k,v in counts.items() if k!='errors'}),flush=True)
  except (OSError,ValueError) as exc:counts['errors'].append({'path':relative,'error_type':type(exc).__name__})
 conn.commit()
 return counts

def load_registry_summary(conn,cutoff,limit=6,project=None):
 """Model-safe summary: no filesystem paths, hashes or unapproved return claims."""
 rows=conn.execute('''SELECT project_key,count(*) AS registered_files,sum(record_count) AS imported_result_rows,
   max(registered_at) AS available_at FROM (
    SELECT DISTINCT ON(relative_path) * FROM research.experiment_artifacts WHERE registered_at<=%s
      AND (%s::text IS NULL OR project_key=%s)
    ORDER BY relative_path,registered_at DESC,artifact_id) latest
   GROUP BY project_key ORDER BY max(registered_at) DESC,project_key LIMIT %s''',(cutoff,project,project,max(1,min(int(limit),20)))).fetchall()
 contracts=load_experiment_contracts(conn,cutoff)
 rows=[{**row,'result_contract':public_experiment_contract(contracts.get(row['project_key']))} for row in rows]
 return {'projects':rows,'usage_status':'research_only','prediction_eligible':False,
  'meaning':'已登记实验来源与结果；登记及校验不等于通过预测质量门槛。'}

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path)
 parser.add_argument('--catalog-only',action='store_true',help='Refresh dataset definitions only; no artifact scan.')
 parser.add_argument('--project',action='append',help='Register only this project under the configured research root; repeatable.')
 args=parser.parse_args()
 try:from .common import connect
 except ImportError:from common import connect
 with connect(autocommit=False) as conn:
  conn.execute("SET statement_timeout='20s'");conn.execute("SET lock_timeout='3s'")
  if not conn.execute('SELECT pg_try_advisory_lock(704201501) AS acquired').fetchone()['acquired']:
   print('Registry already running; no duplicate pass started');return 0
  if args.catalog_only:
   register_catalog(conn);conn.commit();result={'datasets':len(DATASETS),'errors':[]}
  else:result=register(conn,__import__('xtrastock.config',fromlist=['Settings']).Settings().path('paths.research'),args.project)
 result['at']=dt.datetime.now(dt.timezone.utc).isoformat()
 if args.output:args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2))
 print(json.dumps(result,ensure_ascii=False));return 1 if result['errors'] else 0


def public_experiment_contract(contract):
 """Only interpretation metadata reaches models; file locators/hashes stay local."""
 if not isinstance(contract,dict):return {'status':'not_registered','metric_units':{}}
 fields=('contract_version','metric_units','comparison_dimensions','methodology_notes',
         'year_notes','training_status_by_year','model_labels','gate_status')
 return {**{key:contract[key] for key in fields if key in contract},
         'status':'registered','prediction_eligible':False}


def load_experiment_contracts(conn,cutoff):
 rows=conn.execute('''SELECT DISTINCT ON(a.project_key) a.project_key,r.payload
   FROM research.experiment_artifacts a JOIN research.artifact_records r USING(artifact_id)
   WHERE a.registered_at<=%s AND r.row_number=0
     AND a.relative_path='research/'||a.project_key||'/EXPERIMENT_CONTRACT.json'
     AND r.payload->>'contract_version'='1'
   ORDER BY a.project_key,a.registered_at DESC,a.artifact_id''',(cutoff,)).fetchall()
 return {r['project_key']:r['payload'] for r in rows}


def load_experiment_results(conn,cutoff,project=None,limit=8,filters=None,offset=0):
 """Read small result tables, without exposing file paths or promoting research."""
 filters={str(k):str(v) for k,v in (filters or {}).items()}
 if set(filters)-{'year','horizon','cost','cohort','model','allocation'}:raise ValueError('Unsupported experiment filter')
 offset=int(offset)
 if not 0<=offset<=10000:raise ValueError('experiment_offset must be between 0 and 10000')
 records=conn.execute('''WITH latest AS (
   SELECT DISTINCT ON(relative_path) * FROM research.experiment_artifacts
   WHERE registered_at<=%s AND (%s::text IS NULL OR project_key=%s)
   ORDER BY relative_path,registered_at DESC,artifact_id), contracts AS (
    SELECT a.project_key,r.payload,a.registered_at FROM latest a
    JOIN research.artifact_records r USING(artifact_id)
    WHERE a.relative_path='research/'||a.project_key||'/EXPERIMENT_CONTRACT.json'
      AND r.row_number=0 AND r.payload->>'contract_version'='1')
   SELECT a.project_key,a.artifact_id,greatest(a.registered_at,c.registered_at) AS available_at,
          r.row_number,r.payload,c.payload AS result_contract
   FROM latest a JOIN research.artifact_records r USING(artifact_id)
   LEFT JOIN contracts c USING(project_key)
   WHERE a.artifact_kind IN ('csv','tsv') AND a.payload_status='sql_records'
     AND a.relative_path ~* '(costed_results|results|metrics|evaluation)[^/]*[.](csv|tsv)$'
     AND (c.payload IS NULL OR c.payload->'result_artifacts'->>
           substring(a.relative_path FROM length('research/'||a.project_key||'/')+1)=a.content_sha256)
     AND NOT EXISTS(SELECT 1 FROM jsonb_each_text(%s::jsonb) f WHERE
       CASE WHEN f.key IN ('year','horizon','cost')
         AND (r.payload->>f.key) ~ '^-?[0-9]+([.][0-9]+)?$' AND f.value ~ '^-?[0-9]+([.][0-9]+)?$'
       THEN (r.payload->>f.key)::numeric IS DISTINCT FROM f.value::numeric
       ELSE r.payload->>f.key IS DISTINCT FROM f.value END)
   ORDER BY a.project_key,a.relative_path,r.row_number OFFSET %s LIMIT %s''',
   (cutoff,project,project,Jsonb(filters),offset,max(1,min(int(limit),20)))).fetchall()
 out=[]
 for record in records:
  payload=record['payload']
  if not isinstance(payload,dict):continue
  metrics={k:v for k,v in payload.items() if not SECRET.search(k)
           and not any(x in k.lower() for x in ('path','hash','password','sql','url'))
           and isinstance(v,(str,int,float,bool,type(None))) and len(str(v))<=160}
  contract=public_experiment_contract(record.get('result_contract'))
  year=str(payload.get('year',''))
  out.append({'component':'offline_experiment_result','project':record['project_key'],
    'available_at':record['available_at'],'artifact_id':record['artifact_id'],'row_number':record['row_number'],
    'metrics':metrics,'usage_status':'research_only','prediction_eligible':False,
    'metric_units':contract.get('metric_units',{}),
    'contract_status':contract['status'],'gate_status':contract.get('gate_status','unknown'),
    'year_note':contract.get('year_notes',{}).get(year),
    'training_status':contract.get('training_status_by_year',{}).get(year,'unspecified'),
    'methodology_notes':contract.get('methodology_notes',[]),
    'interpretation':'历史样本回测结果；不得当作未来预期收益或生产批准。'})
 return out

if __name__=='__main__':raise SystemExit(main())
