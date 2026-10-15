from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


from xtrastock.config import Settings
ROOT = Settings().path('paths.app')
MODULE_PATH = ROOT / "scripts/search/sync_document_embeddings.py"
SPEC = importlib.util.spec_from_file_location("sync_document_embeddings", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
# The CLI script imports pipeline/common.py as the top-level module `common`.
# Do not leak that alias into unrelated analytics tests in the same process.
loaded_common = sys.modules.get("common")
if loaded_common and "scripts/pipeline/common.py" in str(getattr(loaded_common, "__file__", "")):
    sys.modules.pop("common", None)


class DocumentChunkTests(unittest.TestCase):
    def test_chunk_text_preserves_title_and_bounds_chunks(self):
        body = "第一句介绍服务器。" * 100 + "第二段讨论需求增长。" * 100
        chunks = MODULE.chunk_text("人工智能产业", body, max_chars=320, overlap_chars=40, max_chunks=20)

        self.assertTrue(1 < len(chunks) <= 20)
        self.assertTrue(all(chunk.startswith("人工智能产业\n") for chunk in chunks))
        self.assertTrue(all(len(chunk) <= 320 for chunk in chunks))
        self.assertTrue(any("需求增长" in chunk for chunk in chunks))

    def test_chunk_text_uses_title_for_empty_body(self):
        self.assertEqual(MODULE.chunk_text("只有标题", "", 300, 30, 5), ["只有标题"])

    def test_vector_literal_has_expected_shape(self):
        literal = MODULE.vector_literal([0.1, -0.2, 0.3])
        self.assertTrue(literal.startswith("[") and literal.endswith("]"))
        self.assertEqual(literal.count(","), 2)


if __name__ == "__main__":
    unittest.main()
