"""Hybrid retrieval: BM25 over identifier-aware tokens + dense embeddings, fused with RRF.

Dense embeddings alone handle code poorly (exact symbol names matter), so both rankings are
computed and merged with reciprocal rank fusion. Corpora are small (thousands of chunks), so exact
cosine search over a numpy matrix is used instead of an ANN index.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from ..tools.ast_tools import split_identifier

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "for", "on", "with", "as", "be", "this",
         "that", "self", "def", "return", "import", "from", "none", "true", "false", "if", "else", "not"}


def tokenize(text: str) -> list[str]:
    out = []
    for tok in _TOKEN.findall(text or ""):
        low = tok.lower()
        if low in _STOP:
            continue
        out.append(low)                      # the whole identifier (exact symbol match)
        parts = split_identifier(tok)
        if len(parts) > 1:
            out.extend(p for p in parts if p not in _STOP and len(p) > 1)
    return out


@dataclass
class Doc:
    id: str
    corpus: str          # code | history | docs | ci
    title: str
    text: str
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class BM25:
    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(t) for t in docs_tokens]
        self.len = np.array([len(t) for t in docs_tokens], dtype=float)
        self.avg = float(self.len.mean()) if len(self.len) else 0.0
        df = Counter()
        for t in self.tf:
            df.update(t.keys())
        n = len(docs_tokens)
        self.idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5)) for w, d in df.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        s = np.zeros(len(self.tf))
        for i, tf in enumerate(self.tf):
            denom_base = self.k1 * (1 - self.b + self.b * (self.len[i] / (self.avg or 1)))
            for w in set(query):
                f = tf.get(w)
                if f:
                    s[i] += self.idf.get(w, 0.0) * f * (self.k1 + 1) / (f + denom_base)
        return s


class HashingEmbedder:
    """Offline fallback: signed feature hashing of tokens and character trigrams (L2-normalised)."""

    name = "hashing-512"

    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed(self, texts: list[str], query: bool = False) -> np.ndarray:
        m = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            feats = tokenize(t)
            low = (t or "").lower()
            feats += [low[j:j + 3] for j in range(0, max(0, len(low) - 2), 2)]
            for f in feats:
                h = int(hashlib.md5(f.encode()).hexdigest()[:8], 16)
                m[i, h % self.dim] += 1.0 if (h >> 31) & 1 else -1.0
        n = np.linalg.norm(m, axis=1, keepdims=True)
        n[n == 0] = 1
        return m / n


class GeminiEmbedder:
    """Gemini embeddings with an on-disk cache keyed by content hash."""

    def __init__(self, client, model: str, cache_dir: Path, dim: int = 768):
        self.client, self.model, self.dim = client, model, dim
        self.name = f"{model}-{dim}"
        self.cache_dir = cache_dir / "embeddings" / self.name
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def embed(self, texts: list[str], query: bool = False) -> np.ndarray:
        from google.genai import types

        out: list[np.ndarray | None] = [None] * len(texts)
        todo = []
        for i, t in enumerate(texts):
            h = hashlib.sha256(((("q:" if query else "d:") + t)).encode()).hexdigest()
            p = self.cache_dir / f"{h}.npy"
            if p.exists():
                out[i] = np.load(p)
            else:
                todo.append((i, t, p))
        task = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
        for s in range(0, len(todo), 64):
            batch = todo[s:s + 64]
            resp = self.client.models.embed_content(
                model=self.model, contents=[t[:8000] for _, t, _ in batch],
                config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dim))
            for (i, _, p), e in zip(batch, resp.embeddings):
                v = np.asarray(e.values, dtype=np.float32)
                v /= (np.linalg.norm(v) or 1.0)
                np.save(p, v)
                out[i] = v
        return np.vstack(out) if out else np.zeros((0, self.dim), dtype=np.float32)


class HybridIndex:
    def __init__(self, embedder):
        self.embedder = embedder
        self.docs: list[Doc] = []
        self.bm25: BM25 | None = None
        self.vecs: np.ndarray | None = None

    def add(self, docs: list[Doc]) -> None:
        self.docs.extend(docs)

    def build(self) -> None:
        toks = [tokenize(d.title + "\n" + d.text) for d in self.docs]
        self.bm25 = BM25(toks)
        texts = [f"{d.title}\n{d.text}"[:6000] for d in self.docs]
        self.vecs = self.embedder.embed(texts) if texts else None

    def search(self, query: str, k: int = 5, where: Callable[[Doc], bool] | None = None,
               rrf_k: int = 60) -> list[tuple[Doc, float, dict]]:
        if not self.docs or self.bm25 is None:
            return []
        mask = np.array([where(d) if where else True for d in self.docs])
        if not mask.any():
            return []
        lex = self.bm25.scores(tokenize(query))
        lex[~mask] = -np.inf
        dense = np.full(len(self.docs), -np.inf)
        if self.vecs is not None:
            qv = self.embedder.embed([query], query=True)[0]
            dense = self.vecs @ qv
            dense[~mask] = -np.inf
        ranks: dict[int, dict] = {}
        for name, arr in (("bm25", lex), ("dense", dense)):
            order = [i for i in np.argsort(-arr) if np.isfinite(arr[i]) and (name == "dense" or arr[i] > 0)]
            for r, i in enumerate(order[:50]):
                ranks.setdefault(int(i), {})[name] = r + 1
        fused = []
        for i, rr in ranks.items():
            score = sum(1.0 / (rrf_k + r) for r in rr.values())
            fused.append((self.docs[i], score, {"bm25_rank": rr.get("bm25"), "dense_rank": rr.get("dense"),
                                                 "bm25": float(lex[i]) if np.isfinite(lex[i]) else None,
                                                 "cosine": float(dense[i]) if np.isfinite(dense[i]) else None}))
        fused.sort(key=lambda x: -x[1])
        return fused[:k]

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "docs.json").write_text(json.dumps([d.to_dict() for d in self.docs]))
        if self.vecs is not None:
            np.save(path / "vecs.npy", self.vecs)
