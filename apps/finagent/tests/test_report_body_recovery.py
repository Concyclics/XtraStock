import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/pipeline'))
from backfill_missing_report_bodies import identity_supported
class IdentityTests(unittest.TestCase):
 def test_exact_title_ignores_pdf_whitespace(self):
  row={'report_title':'人工智能服务器产业深度研究','report_type':'industry'}
  self.assertEqual(identity_supported('人工智能 服务器产业\n深度研究',row),'exact_report_title')
 def test_stock_code_requires_original_stock_report(self):
  row={'report_title':'公司深度研究长标题','report_type':'stock','stock_code':'600000'}
  self.assertEqual(identity_supported('股票代码：600000',row),'original_infocode_url_and_security_code')
  with self.assertRaises(ValueError):identity_supported('代码16000001',row)
  with self.assertRaises(ValueError):identity_supported('股票代码：600000',{**row,'report_type':'industry'})
 def test_unrelated_pdf_is_not_ingested(self):
  with self.assertRaises(ValueError):identity_supported('其他公司的新闻报道',{'report_title':'人工智能产业深度研究','report_type':'industry'})
 def test_cover_subtitle_requires_original_id_and_publication_date(self):
  row={'report_title':'银行行业周报：中特估催化板块行情，关注估值重塑机遇','report_type':'industry','eastmoney_infocode':'AP202305081586356668','report_date':'2023-05-08'}
  cover='银行 2023年5月8日\n中特估催化板块行情，关注估值重塑机遇'
  self.assertEqual(identity_supported(cover,row),'original_infocode_cover_subtitle_and_date')
  for invalid in [cover.replace('2023年5月8日','2022年5月8日'),'前文'*1000+cover,cover.replace('2023年5月8日','')]:
   with self.assertRaises(ValueError):identity_supported(invalid,row)
  with self.assertRaises(ValueError):identity_supported(cover,{**row,'eastmoney_infocode':'unknown'})
if __name__=='__main__':unittest.main()
