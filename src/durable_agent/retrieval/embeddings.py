"""Embedding backends behind one small protocol.

``SentenceTransformerEmbedder`` is the real one (all-MiniLM-L6-v2 by default: 384 dims,
~90 MB, runs on CPU in milliseconds per chunk, costs zero API tokens). ``HashEmbedder`` is a
deterministic bag-of-words embedder used by the test suite so CI never downloads a model.
Both produce L2-normalized vectors, so LanceDB's cosine distance is meaningful for either.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """Lazy-loads the model on first use so importing this module stays cheap."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.name = model_name
        self._model: Any = None  # sentence_transformers ships no type information
        self.dim = 0

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.name, device="cpu")
            self.dim = int(self._model.get_embedding_dimension() or 0)
        return self._model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(list(texts), normalize_embeddings=True, convert_to_numpy=True)
        return [[float(x) for x in row] for row in vectors]


class HashEmbedder:
    """Hashed bag-of-words. Not semantic, but deterministic and lexically meaningful."""

    _token = re.compile(r"[a-z0-9_]+")

    def __init__(self, dim: int = 256) -> None:
        self.name = f"hash-{dim}"
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in self._token.findall(text.lower()):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)  # noqa: S324 - not security
                vec[h % self.dim] += 1.0 if (h >> 8) % 2 else -1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out
