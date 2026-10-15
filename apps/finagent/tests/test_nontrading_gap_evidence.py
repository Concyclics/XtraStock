import copy,datetime as dt,hashlib,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts/analytics'))
from resolve_market_nontrading_gaps import validate_item
class NontradingEvidenceTests(unittest.TestCase):
 def setUp(self):
  body='证券代码：600491；公司股票自2026年8月27日停牌一天，于2026年8月28日复牌。';self.doc={'body':body,'stock_code':'600491','source_url':'https://static.cninfo.com.cn/a.pdf','announced_at':dt.datetime(2026,8,26,16,tzinfo=dt.timezone.utc)}
  self.item={'stock_code':'600491','interval_start':'2026-08-27','resume_date':'2026-08-28','missing_dates':['2026-08-27'],'method':'closed_suspension_interval','manual_review':'checked','sources':[]}
  for event,day in [('suspend','2026-08-27'),('resume','2026-08-28')]:self.item['sources'].append({'event':event,'event_date':day,'document_id':'d','body_sha256':hashlib.sha256(body.encode()).hexdigest(),'source_url':self.doc['source_url'],'public_available_at':self.doc['announced_at'].isoformat(),'quote_normalized':body})
 def test_exact_proof_does_not_need_fabricated_price(self):self.assertEqual(validate_item(self.item,{'d':self.doc}),self.doc['announced_at'])
 def test_resume_day_cannot_be_excused_as_missing(self):
  self.item['missing_dates'].append('2026-08-28')
  with self.assertRaisesRegex(ValueError,'outside proven'):validate_item(self.item,{'d':self.doc})
 def test_changed_body_rejected(self):
  self.doc['body']+='changed'
  with self.assertRaisesRegex(ValueError,'body changed'):validate_item(self.item,{'d':self.doc})
 def test_wrong_security_rejected(self):
  self.doc['stock_code']='600492'
  with self.assertRaisesRegex(ValueError,'identity mismatch'):validate_item(self.item,{'d':self.doc})
 def test_unproven_quote_rejected(self):
  self.item['sources'][0]['quote_normalized']='missing quote'
  with self.assertRaisesRegex(ValueError,'quote absent'):validate_item(self.item,{'d':self.doc})
 def test_single_day_requires_explicit_source_duration(self):
  self.item['resume_date']=None;self.item['method']='explicit_single_day';self.item['sources']=self.item['sources'][:1]
  self.assertEqual(validate_item(self.item,{'d':self.doc}),self.doc['announced_at'])
if __name__=='__main__':unittest.main()
