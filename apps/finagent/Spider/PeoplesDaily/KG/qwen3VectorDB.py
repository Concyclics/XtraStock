# qwen3VectorDB.py
import os
import tempfile
from typing import List, Optional, Sequence, Dict, Any, Union, Callable

import faiss
import numpy as np
import pandas as pd
import torch
from sentence_transformers import SentenceTransformer


class Qwen3VectorDB:
    """
    Persistent vector database for RAG using Qwen3 embeddings + FAISS.
    - Safe GPU→CPU→float32 conversion before FAISS add
    - Robust retry on Triton/Inductor OOM (batch halving + CPU fallback)
    - Uses SDPA mem-efficient kernels (Flash disabled) via inline context
    - HNSW or Flat IP index types
    """

    def __init__(
        self,
        path: str,
        model_name: str = "Qwen/Qwen3-Embedding-0.6B",
        index_type: str = "hnsw",        # "hnsw" or "flat"
        hnsw_m: int = 64,
        batch_size: int = 32,
        device: Optional[str] = None,    # "cuda", "cpu", "mps", or None (auto)
        trust_remote_code: bool = True,
        pad_left: bool = True,
        verbose: bool = True,
    ):
        self.path = path
        self.model_name = model_name
        self.index_type = index_type.lower()
        self.hnsw_m = int(hnsw_m)
        self.batch_size = int(batch_size)
        self.verbose = verbose

        os.makedirs(self.path, exist_ok=True)
        self.indexPath = os.path.join(self.path, "index.faiss")
        self.dataPath = os.path.join(self.path, "data.csv")

        # -------- Device selection --------
        if device is None:
            if torch.cuda.is_available():
                device = "cuda"
            elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
        self.device = device

        # -------- Model load --------
        model_kwargs = {"trust_remote_code": trust_remote_code}
        tokenizer_kwargs = {"padding_side": "left" if pad_left else "right"}

        if self.verbose:
            print(f"[VectorDB] Loading model: {model_name} on device={self.device}")
        self.model = SentenceTransformer(
            model_name,
            model_kwargs=model_kwargs,
            tokenizer_kwargs=tokenizer_kwargs,
            device=self.device,
        )

        self.dim = self.model.get_sentence_embedding_dimension()
        if self.verbose:
            print(f"[VectorDB] Embedding dim: {self.dim}")

        self.index = self._load_or_create_index()
        self.data = self._load_or_create_data()

    # ---------- Persistence ----------
    def _atomic_write_bytes(self, path: str, writer_fn: Callable[[str], None]):
        dir_name = os.path.dirname(path)
        with tempfile.NamedTemporaryFile(delete=False, dir=dir_name) as tmp:
            tmp_path = tmp.name
            writer_fn(tmp_path)
        os.replace(tmp_path, path)

    def _save_index(self):
        if self.verbose:
            print(f"[VectorDB] Saving FAISS index -> {self.indexPath} (ntotal={self.index.ntotal})")
        def _writer(tmp_path):
            faiss.write_index(self.index, tmp_path)
        self._atomic_write_bytes(self.indexPath, _writer)

    def _save_data(self):
        if self.verbose:
            print(f"[VectorDB] Saving metadata CSV -> {self.dataPath} (rows={len(self.data)})")
        dir_name = os.path.dirname(self.dataPath)
        with tempfile.NamedTemporaryFile("w", delete=False, dir=dir_name, newline="", encoding="utf-8") as tmp:
            self.data.to_csv(tmp.name, index=False)
            tmp_path = tmp.name
        os.replace(tmp_path, self.dataPath)

    def save(self):
        self._save_index()
        self._save_data()

    # ---------- Loaders ----------
    def _load_or_create_index(self):
        if os.path.exists(self.indexPath):
            if self.verbose:
                print(f"[VectorDB] Loading FAISS index from {self.indexPath}")
            idx = faiss.read_index(self.indexPath)
            try:
                if idx.d != self.dim:
                    if self.verbose:
                        print(f"[VectorDB] Index dim {idx.d} != model dim {self.dim}, rebuilding empty index.")
                    return self._new_index()
            except Exception:
                pass
            return idx
        else:
            if self.verbose:
                print("[VectorDB] No index found, creating new.")
            return self._new_index()

    def _new_index(self):
        if self.index_type == "flat":
            return faiss.IndexFlatIP(self.dim)
        elif self.index_type == "hnsw":
            idx = faiss.IndexHNSWFlat(self.dim, self.hnsw_m, faiss.METRIC_INNER_PRODUCT)
            try:
                idx.hnsw.efConstruction = 200
            except Exception:
                pass
            return idx
        else:
            raise ValueError("index_type must be 'hnsw' or 'flat'")

    def _load_or_create_data(self):
        if os.path.exists(self.dataPath):
            if self.verbose:
                print(f"[VectorDB] Loading CSV metadata from {self.dataPath}")
            df = pd.read_csv(self.dataPath)
            if not {"id", "document"}.issubset(df.columns):
                if self.verbose:
                    print("[VectorDB] CSV missing required columns; reinitializing.")
                return pd.DataFrame(columns=["id", "document"])
            return df
        else:
            if self.verbose:
                print("[VectorDB] No CSV found, creating new.")
            return pd.DataFrame(columns=["id", "document"])

    # ---------- Utils ----------
    def _clear_cuda_cache(self):
        if self.device == "cuda":
            torch.cuda.empty_cache()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.save()
        self.close()

    def close(self):
        try: del self.model
        except: pass
        try: del self.index
        except: pass
        try: del self.data
        except: pass
        self._clear_cuda_cache()

    # ---------- Robust encode (no persistent context) ----------
    def _encode_with_retries(
        self,
        texts: Sequence[str],
        batch_size: int,
        normalize: bool,
        show_progress: bool,
    ) -> np.ndarray:
        """
        Encode with retries:
        - On RuntimeError (e.g., Triton/Inductor OOM), halve batch and retry.
        - If still failing on CUDA with bs=1, fall back to CPU for this chunk.
        - Returns numpy array (dtype preserved; we cast to float32 later).
        """
        bs = max(1, int(batch_size))
        on_cuda = (self.device == "cuda")
        tried_cpu = False

        while True:
            try:
                with torch.no_grad():
                    if self.device == "cuda":
                        # Create context inline each time — Flash off, mem-efficient on
                        with torch.backends.cuda.sdp_kernel(
                            enable_flash=False, enable_math=True, enable_mem_efficient=True
                        ):
                            embs = self.model.encode(
                                list(texts),
                                batch_size=bs,
                                show_progress_bar=False if not show_progress else (bs >= 8),
                                normalize_embeddings=normalize,
                                convert_to_numpy=True,
                            )
                    else:
                        embs = self.model.encode(
                            list(texts),
                            batch_size=bs,
                            show_progress_bar=False if not show_progress else (bs >= 8),
                            normalize_embeddings=normalize,
                            convert_to_numpy=True,
                        )
                return np.asarray(embs)
            except RuntimeError as e:
                msg = str(e)
                if self.verbose:
                    print(f"[VectorDB] Encode error at bs={bs}: {e.__class__.__name__}: {msg.splitlines()[0]}")

                # 1) halve batch size if possible
                if bs > 1:
                    bs = max(1, bs // 2)
                    if self.verbose:
                        print(f"[VectorDB] Retrying with smaller batch size: {bs}")
                    self._clear_cuda_cache()
                    continue

                # 2) fall back to CPU once
                if on_cuda and not tried_cpu:
                    if self.verbose:
                        print("[VectorDB] Falling back to CPU for this chunk.")
                    tried_cpu = True
                    cpu_model = SentenceTransformer(
                        self.model_name,
                        model_kwargs={"trust_remote_code": True},
                        tokenizer_kwargs={"padding_side": "left"},
                        device="cpu",
                    )
                    cpu_embs = cpu_model.encode(
                        list(texts),
                        batch_size=4,
                        show_progress_bar=False,
                        normalize_embeddings=normalize,
                        convert_to_numpy=True,
                    )
                    return np.asarray(cpu_embs)

                # 3) bubble up
                raise

    # ---------- Add documents ----------
    def add_documents(
        self,
        documents: Sequence[str],
        batch_size: Optional[int] = None,
        normalize: bool = True,
        show_progress: bool = True,
    ) -> int:
        if not documents:
            return 0

        bs = int(batch_size or self.batch_size)
        n_total = len(documents)
        next_id_start = len(self.data)

        new_rows = pd.DataFrame(
            {"id": np.arange(next_id_start, next_id_start + n_total, dtype=np.int64),
             "document": list(documents)}
        )
        self.data = pd.concat([self.data, new_rows], ignore_index=True)

        if self.verbose:
            print(f"[VectorDB] Indexing {n_total} docs in batches of {bs} (normalize={normalize})")

        added = 0
        for start in range(0, n_total, bs):
            end = min(start + bs, n_total)
            chunk = documents[start:end]

            embs = self._encode_with_retries(
                texts=chunk,
                batch_size=bs,
                normalize=normalize,
                show_progress=show_progress,
            )

            # ✅ Always convert to CPU float32 for FAISS
            if isinstance(embs, torch.Tensor):
                embs = embs.detach().cpu().numpy()
            embs = np.asarray(embs, dtype=np.float32)
            if not embs.flags["C_CONTIGUOUS"]:
                embs = np.ascontiguousarray(embs)

            self.index.add(embs)
            added += embs.shape[0]
            self._clear_cuda_cache()

            if self.verbose:
                print(f"[VectorDB] Added {end}/{n_total}")

        return added

    # ---------- Search ----------
    def search(self, query, k=10, query_prompt=None):
        """
        Qwen3-style search:
        - If query_prompt is given → use prompt=query_prompt
        - Else → use prompt_name="query"
        - Uses SDPA mem-efficient kernels on CUDA
        - Converts embedding to float32 for FAISS
        """
        if self.index is None or self.index.ntotal == 0 or len(self.data) == 0:
            return []

        # Encode query
        with torch.no_grad():
            if self.device == "cuda":
                with torch.backends.cuda.sdp_kernel(enable_flash=False, enable_math=True, enable_mem_efficient=True):
                    if query_prompt:
                        query_embedding = self.model.encode([query], prompt=query_prompt, convert_to_numpy=True)[0]
                    else:
                        query_embedding = self.model.encode([query], prompt_name="query", convert_to_numpy=True)[0]
            else:
                if query_prompt:
                    query_embedding = self.model.encode([query], prompt=query_prompt, convert_to_numpy=True)[0]
                else:
                    query_embedding = self.model.encode([query], prompt_name="query", convert_to_numpy=True)[0]

        # ✅ Convert to float32 and contiguous for FAISS
        query_embedding = np.asarray(query_embedding, dtype=np.float32)[None, :]
        if not query_embedding.flags["C_CONTIGUOUS"]:
            query_embedding = np.ascontiguousarray(query_embedding)

        distances, indices = self.index.search(query_embedding, k)

        results = []
        for i in range(len(indices[0])):
            idx = indices[0][i]
            if idx != -1:
                results.append({
                    "id": int(idx),
                    "document": self.data.iloc[idx]["document"],
                    "distance": float(distances[0][i]),
                })
        return results


    # ---------- Maintenance ----------
    def rebuild_index(self):
        if len(self.data) == 0:
            self.index = self._new_index()
            return 0
        if self.verbose:
            print("[VectorDB] Rebuilding index from CSV...")
        self.index = self._new_index()
        docs = self.data["document"].astype(str).tolist()
        n = self.add_documents(docs)
        if self.verbose:
            print(f"[VectorDB] Rebuild complete. ntotal={self.index.ntotal}")
        return n

    def count(self) -> int:
        return int(self.index.ntotal if self.index is not None else 0)

    @staticmethod
    def default(path: str = "./qwen3_index") -> "Qwen3VectorDB":
        return Qwen3VectorDB(
            path=path,
            model_name="Qwen/Qwen3-Embedding-0.6B",
            index_type="hnsw",
            hnsw_m=64,
            batch_size=32,
            device=None,
            trust_remote_code=True,
            pad_left=True,
            verbose=True,
        )
