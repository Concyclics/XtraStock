import csv,hashlib,importlib.util,json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/analytics'))
spec=importlib.util.spec_from_file_location('listing_import',ROOT/'scripts/analytics/import_security_master_evidence.py')
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
class ListingEvidenceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'source.json'
  self.body={'result':[{'SECURITY_CODE_A':'600000','SECURITY_ABBR_A':'浦发银行','LISTING_DATE':'1999-11-10'}]};self.write()
 def tearDown(self):self.tmp.cleanup()
 def write(self):self.path.write_text(json.dumps(self.body),encoding='utf-8')
 def parse(self,**kw):
  args=dict(kind='sse_main',url='https://query.sse.com.cn/security/stock/list',observed_at='2026-10-13T10:00:00+00:00');args.update(kw)
  return mod.parsed_source(self.path,**args)
 def test_official_date_and_exact_hash(self):
  rows,errors=self.parse(expected_sha=hashlib.sha256(self.path.read_bytes()).hexdigest())
  self.assertEqual(errors,[]);self.assertEqual(rows[0]['listed_date'],'1999-11-10')
  self.assertEqual(mod.normalized_date('20200102'),'2020-01-02')
 def test_wrong_hash_rejected(self):
  with self.assertRaisesRegex(ValueError,'hash mismatch'):self.parse(expected_sha='0'*64)
 def test_spoof_domain_and_naive_time_rejected(self):
  with self.assertRaisesRegex(ValueError,'official exchange'):self.parse(url='https://sse.com.cn.attacker.example/list')
  with self.assertRaisesRegex(ValueError,'timezone'):self.parse(observed_at='2026-10-13T10:00:00')
 def test_conflicting_dates_rejected(self):
  self.body['result'].append({**self.body['result'][0],'LISTING_DATE':'1998-11-10'});self.write()
  with self.assertRaisesRegex(ValueError,'conflicting listing'):self.parse()
 def test_manifest_cannot_invent_date(self):
  rows,_=self.parse();r={k:v for k,v in rows[0].items() if k!='source_row'};r['listed_date']='1990-01-01'
  target=Path(self.tmp.name)/'request.csv'
  with target.open('w') as f:
   w=csv.DictWriter(f,fieldnames=list(r));w.writeheader();w.writerow(r)
  with self.assertRaisesRegex(ValueError,'not supported'):mod.manifest_rows(target)
if __name__=='__main__':unittest.main()
