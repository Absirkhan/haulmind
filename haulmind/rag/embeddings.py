"""Deterministic hashed-feature embeddings (no model download, ~zero RAM).

Tokens and bigrams are hashed into a fixed-size vector with TF-IDF-style weights and L2
normalisation, so cosine similarity on a FAISS inner-product index behaves like a sparse
lexical retriever. For a corpus of a few dozen technical docs where fault codes and component
names dominate, this is competitive with small neural embedders and costs nothing.

An optional Gemini embedding backend sits behind the same ``Embedder`` protocol.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from itertools import pairwise
from typing import Any, Protocol

import numpy as np

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]*")
STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "be",
        "by",
        "at",
        "from",
        "as",
        "that",
        "this",
        "it",
        "its",
        "if",
        "then",
        "than",
        "into",
        "over",
        "under",
        "when",
        "while",
        "may",
        "can",
        "should",
        "will",
        "not",
        "no",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
    ]
)


def tokenize(text: str) -> list[str]:
    toks = [t for t in TOKEN_RE.findall(text.lower()) if t not in STOP and len(t) > 1]
    bigrams = [f"{a}_{b}" for a, b in pairwise(toks)]
    return toks + bigrams


def _slot(token: str, dim: int) -> tuple[int, float]:
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    idx = int.from_bytes(h[:4], "little") % dim
    sign = 1.0 if h[4] & 1 else -1.0
    return idx, sign


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashedEmbedder:
    """TF-IDF weighted feature hashing. ``idf`` is fitted on the corpus at index-build time."""

    def __init__(self, dim: int = 512, idf: dict[str, float] | None = None) -> None:
        self.dim = dim
        self.idf: dict[str, float] = idf or {}
        self.default_idf = max(self.idf.values(), default=1.0)

    @classmethod
    def fit(cls, docs: Iterable[str], dim: int = 512) -> HashedEmbedder:
        df: dict[str, int] = {}
        n = 0
        for d in docs:
            n += 1
            for t in set(tokenize(d)):
                df[t] = df.get(t, 0) + 1
        idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        return cls(dim=dim, idf=idf)

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            tf: dict[str, int] = {}
            for t in tokenize(text):
                tf[t] = tf.get(t, 0) + 1
            for t, c in tf.items():
                idx, sign = _slot(t, self.dim)
                w = (1.0 + math.log(c)) * self.idf.get(t, self.default_idf)
                out[row, idx] += sign * w
            norm = float(np.linalg.norm(out[row]))
            if norm > 0:
                out[row] /= norm
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"dim": self.dim, "idf": self.idf}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HashedEmbedder:
        return cls(dim=int(d["dim"]), idf=dict(d["idf"]))
