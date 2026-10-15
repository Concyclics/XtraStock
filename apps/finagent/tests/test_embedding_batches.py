import contextlib
import datetime as dt
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts/search'))
import batch_document_embeddings as batch
import sync_document_embeddings as worker


class EmbeddingBatchTests(unittest.TestCase):
    def test_whole_long_document_includes_tail_beyond_old_200_chunk_limit(self):
        body='财务报告当期收入。'*20000+'仅在文末的唯一金额92837819.67'
        doc=batch.prepare({'title':'完整半年报','body':body},worker)
        self.assertGreater(len(doc['chunks']),200)
        self.assertIn('92837819.67',doc['chunks'][-1])

    def test_long_title_does_not_prevent_chunker_advancement(self):
        doc=batch.prepare({'title':'标题'*1000,'body':'正文'*1500+'最后一段'},worker)
        self.assertIn('最后一段',doc['chunks'][-1])
        self.assertLess(len(doc['chunks']),20)

    def test_complete_whitespace_heavy_tail_is_accepted(self):
        body=('审计正文。'*180)+'\n'+('原件一致'+' '*60)*25
        doc=batch.prepare({'title':'审计报告','body':body},worker)
        self.assertTrue(doc['chunks'])
        self.assertTrue(doc['chunks'][-1].endswith('原件一致'))

    def test_actual_chunk_limit_truncation_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'uncovered'):
            worker.chunk_text('报告','正文。'*1000,900,120,1,require_complete=True)

    def test_cross_document_vector_assignment_preserves_boundaries(self):
        documents=[{'chunks':['a','b']},{'chunks':['c']},{'chunks':['d','e']}]
        vectors=[[1,0],[0,1],[.2,.8],[.4,.6],[.5,.5]]
        batch.assign_vectors(documents,vectors,2)
        self.assertEqual(documents[0]['vectors'],vectors[:2])
        self.assertEqual(documents[1]['vectors'],vectors[2:3])
        self.assertEqual(documents[2]['vectors'],vectors[3:])
        with self.assertRaises(ValueError):batch.assign_vectors(documents,vectors[:-1],2)
        with self.assertRaises(ValueError):batch.assign_vectors(documents,[[math.nan,0]]*5,2)
        with self.assertRaises(ValueError):batch.assign_vectors(documents,[[1]]*5,2)

    def test_group_limit_does_not_split_or_lose_long_documents(self):
        docs=[{'chunks':['x']*n} for n in (10,20,600,5)]
        groups=list(batch.groups(docs,128))
        self.assertEqual([len(g) for g in groups],[2,1,1])
        self.assertEqual([d for g in groups for d in g],docs)

    def test_provider_indexes_must_be_unique_within_each_request(self):
        response=Mock();response.json.return_value={'data':[{'index':0,'embedding':[1,0]},{'index':0,'embedding':[0,1]}]}
        with patch.dict(os.environ,{worker.EMBEDDING_API_KEY_ENV:'unit-test-only'}),patch.object(worker.requests,'post',return_value=response):
            with self.assertRaisesRegex(RuntimeError,'indexes'):worker.remote_embed(['a','b'],128)
            response.json.return_value={'data':[{'index':1,'embedding':[0,1]},{'index':0,'embedding':[1,0]}]}
            self.assertEqual(worker.remote_embed(['a','b'],128),[[1,0],[0,1]])

    def test_changed_source_or_reclaimed_lease_is_never_overwritten(self):
        stamp=dt.datetime.now(dt.timezone.utc)
        doc={'id':'id','updated_at':stamp,'lease_at':stamp}
        for current,lease in ((stamp+dt.timedelta(seconds=1),stamp),(stamp,stamp+dt.timedelta(seconds=1))):
            conn=Mock();conn.transaction.return_value=contextlib.nullcontext()
            conn.execute.side_effect=[Mock(),Mock(),Mock(fetchall=lambda:[{'id':'id','updated_at':current}]),
                Mock(fetchall=lambda:[{'document_id':'id','started_at':lease,'status':'running'}])]
            self.assertEqual(batch.store(conn,[doc],worker),[])
            self.assertFalse(any('DELETE' in call.args[0] for call in conn.execute.call_args_list))


if __name__=='__main__':unittest.main()
