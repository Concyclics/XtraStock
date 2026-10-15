import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/pipeline'))
import sync_cninfo_document_bodies as bodies


class OfficialAttachmentRetry(unittest.TestCase):
    def test_invalid_download_is_preserved_and_replaced_only_after_parse(self):
        import fitz
        with fitz.open() as doc:
            page=doc.new_page()
            page.insert_text((40,40),'Official announcement body with verified source. '*5)
            valid=doc.tobytes()
        for response_bytes, succeeds in ((valid,True),(b'%PDF-1.7\nbroken again',False)):
            with self.subTest(succeeds=succeeds),tempfile.TemporaryDirectory() as temp:
                path=Path(temp)/'1225568317.pdf'
                original=b'%PDF-1.7\ntruncated';path.write_bytes(original)
                row={'body':'','file_path':str(path),'external_id':'1225568317',
                     'url':'https://static.cninfo.com.cn/finalpage/2026-09-17/1225568317.PDF',
                     'published_at':dt.datetime(2026,9,17),'metadata':{}}
                response=MagicMock();response.__enter__.return_value=response
                response.is_redirect=False;response.iter_content.return_value=[response_bytes]
                with patch.object(bodies.requests,'Session') as session:
                    session.return_value.get.return_value=response
                    if succeeds:
                        result=bodies.acquire(row)
                        self.assertIn('Official announcement',result['text'])
                        self.assertEqual(path.read_bytes(),valid)
                    else:
                        with self.assertRaisesRegex(ValueError,'pdftotext_failed'):
                            bodies.acquire(row)
                        self.assertEqual(path.read_bytes(),original)
                backups=list(Path(temp).glob('*.invalid-*'))
                self.assertEqual(len(backups),1)
                self.assertEqual(backups[0].read_bytes(),original)


if __name__=='__main__':unittest.main()
