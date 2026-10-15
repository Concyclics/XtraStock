import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/search"))
import sync_document_embeddings as worker


class EmbeddingOutageTests(unittest.TestCase):
    def run_worker(self, responses):
        claimed = [{"document_id": value} for value in ("a", "b", "c")]
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", ["sync", "--limit", "3"]))
            stack.enter_context(mock.patch.object(worker, "EMBEDDING_PROVIDER", "openai_compatible"))
            stack.enter_context(mock.patch.object(worker, "MODEL_DIMENSIONS", 2))
            stack.enter_context(mock.patch.object(worker, "claim_documents", return_value=claimed))
            stack.enter_context(mock.patch.object(worker, "fetch_document", side_effect=lambda key: {"id": key, "title": "公司", "body": "公告正文"}))
            stack.enter_context(mock.patch.object(worker, "chunk_text", return_value=["公告正文"]))
            stack.enter_context(mock.patch.object(worker, "reuse_unchanged_vectors", return_value=False))
            remote = stack.enter_context(mock.patch.object(worker, "remote_embed", side_effect=responses))
            stored = stack.enter_context(mock.patch.object(worker, "store_document"))
            failed = stack.enter_context(mock.patch.object(worker, "mark_failed"))
            released = stack.enter_context(mock.patch.object(worker, "release_unprocessed_claims"))
            stack.enter_context(mock.patch.object(worker, "refresh_embedding_watermark"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            code = worker.main()
        return code, remote, stored, failed, released

    def http_error(self, status):
        response = requests.Response()
        response.status_code = status
        return requests.HTTPError("backend unavailable", response=response)

    def test_503_stops_batch_and_does_not_consume_unattempted_retries(self):
        code, remote, stored, failed, released = self.run_worker([self.http_error(503)])
        self.assertEqual(1, code)
        self.assertEqual(1, remote.call_count)
        self.assertEqual(1, failed.call_count)
        stored.assert_not_called()
        released.assert_called_once_with([{"document_id": "b"}, {"document_id": "c"}])

    def test_document_specific_error_does_not_skip_other_documents(self):
        code, remote, stored, failed, released = self.run_worker([self.http_error(400), [[1, 0]], [[0, 1]]])
        self.assertEqual(1, code)
        self.assertEqual(3, remote.call_count)
        self.assertEqual(2, stored.call_count)
        self.assertEqual(1, failed.call_count)
        released.assert_not_called()

    def test_budget_defers_claims_without_consuming_retry(self):
        code,remote,stored,failed,released=self.run_worker([worker.EmbeddingBudgetExhausted()])
        self.assertEqual(code,0)
        stored.assert_not_called();failed.assert_not_called()
        released.assert_called_once_with([{"document_id":"a"},{"document_id":"b"},{"document_id":"c"}])

    def test_disconnect_and_rate_limit_are_transient(self):
        for error in (requests.Timeout(), requests.ConnectionError(), self.http_error(429)):
            self.assertTrue(worker.service_unavailable(error))
        self.assertFalse(worker.service_unavailable(ValueError("malformed document")))


if __name__ == "__main__":
    unittest.main()
