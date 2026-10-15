import datetime as dt
import sys,unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/pipeline'))
import daily_cninfo_filings as m

class PaginationTests(unittest.TestCase):
 def fetch(self,payloads):
  with patch.object(m,'fetch_page',side_effect=payloads),patch.object(m.time,'sleep'):
   return m.fetch_range(None,dt.date(2026,7,1),dt.date(2026,10,12),2,30,'688825')
 def test_empty_page_retries_instead_of_truncating(self):
  first={'totalAnnouncement':3,'announcements':[{'announcementId':'a'},{'announcementId':'b'}]}
  total,rows,_=self.fetch([first,{'announcements':[]},{'announcements':[{'announcementId':'c'}]}])
  self.assertEqual(total,3);self.assertEqual(len(rows),3)
 def test_repeated_page_fails_instead_of_claiming_complete(self):
  first={'totalAnnouncement':3,'announcements':[{'announcementId':'a'},{'announcementId':'b'}]}
  with self.assertRaisesRegex(RuntimeError,'no progress'):self.fetch([first,first,first,first])
 def test_empty_first_page_and_underfilled_page_fail_closed(self):
  with self.assertRaisesRegex(RuntimeError,'empty first'):self.fetch([{'totalAnnouncement':3}])
  with self.assertRaisesRegex(RuntimeError,'count mismatch'):
   self.fetch([{'totalAnnouncement':4,'announcements':[{'announcementId':'a'},{'announcementId':'b'}]},
    {'announcements':[{'announcementId':'c'}]}])
 def test_highlight_is_removed_from_issuer_name(self):
  self.assertEqual(m.clean_title('<em>长鑫科技</em>'),'长鑫科技')
if __name__=='__main__':unittest.main()
