"""
Embedders -- text -> vector.

Two implementations live behind the Embedder Protocol:

  * SentenceTransformerEmbedder   the production embedder. Wraps
                                  sentence-transformers' all-MiniLM-L12-v2 by
                                  default (~120MB on disk, runs locally on CPU,
                                  free, no data leaves the host). The
                                  sentence-transformers import is LAZY so
                                  importing this module does not require the
                                  dependency to be installed.

  * FakeEmbedder                  a deterministic content-aware embedder for
                                  tests. Same text -> same vector, different
                                  text -> different vector. Does NOT model real
                                  semantic similarity (that is what
                                  sentence-transformers does and is not the
                                  unit under test in our suite); identity is
                                  enough to verify that the vector store is
                                  routing the right rows back.

Both expose embed(texts) -> list[list[float]] of the configured dimension.
"""
from __future__ import annotations

import hashlib
from typing import Protocol


class Embedder(Protocol):
    """Anything that turns a list of texts into a list of equal-length vectors."""

    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


# ---------------------------------------------------------------------------
# Production: sentence-transformers (lazy import)
# ---------------------------------------------------------------------------


class SentenceTransformerEmbedder:
    """
    Wraps sentence-transformers. Lazy-imports the dependency on first use so
    code that never needs real embeddings (CI, FakeEmbedder users) does not
    have to install ~500 MB of ML wheels.
    """

    # all-MiniLM-L12-v2 outputs 384-dim embeddings.
    dim = 384

    def __init__(self, model_name: str = "all-MiniLM-L12-v2") -> None:
        self.model_name = model_name
        self._model = None  # populated on first embed()

    def _load(self) -> None:
        if self._model is None:
            # Lazy import: failing here means the operator needs to
            # `pip install sentence-transformers` -- the rest of the toolkit
            # (and the FakeEmbedder-using tests) is unaffected by that gap.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._load()
        # convert_to_numpy=True so we can .tolist() to plain Python floats --
        # chromadb expects List[float], not numpy arrays.
        vectors = self._model.encode(texts, convert_to_numpy=True)
        return [v.tolist() for v in vectors]


# ---------------------------------------------------------------------------
# Test: deterministic content-aware fake
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """
    Deterministic embedder for tests. SHA-256 of each text seeds a fixed-length
    float vector. Properties we rely on:

      1. Same text -> exact same vector (round-trip / identity retrieval).
      2. Different text -> different vector (no false collisions).
      3. Stable across processes (hash is content-based, no RNG state).

    Properties we do NOT promise:
      * Semantic similarity. "GDPR" and "privacy" map to unrelated vectors
        here. That is by design -- we are testing the plumbing, not the
        sentence-transformers model.
    """

    def __init__(self, dim: int = 384) -> None:
        if dim < 4:
            raise ValueError("dim must be >= 4")
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            # Expand SHA-256 to fill the vector by repeated re-hashing with
            # a counter so the vector is stable, deterministic, and uses the
            # full input content.
            material = b""
            seed = t.encode("utf-8")
            counter = 0
            while len(material) < self.dim:
                material += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
                counter += 1
            # Map each byte to [-1, 1] so the resulting vector looks like
            # something a real embedder might produce (signed components).
            vec = [(b / 127.5) - 1.0 for b in material[: self.dim]]
            out.append(vec)
        return out
