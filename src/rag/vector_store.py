"""
ChromaDB-backed vector store -- one collection per framework.

Design constraints inherited from v0:
  * SQLite stays the source of truth. Chroma is a *derived* index that can be
    rebuilt from SQLite at any time. If the two ever drift, SQLite wins.
  * Every read and every write is wrapped in `gateway.access()` so the
    audit-before-access discipline that protects SQLite also protects the
    vector store. The audit row is written BEFORE the Chroma call.
  * The embedder is injected. No production code paths instantiate it inline;
    tests pass FakeEmbedder, production wires SentenceTransformerEmbedder via
    `default_embedder()`.

Persistence mode:
  * `persist_dir=Path(...)`  -> on-disk PersistentClient (production).
  * `persist_dir=None`       -> in-memory EphemeralClient (tests).

Collection naming:
  Chroma collection names must be ASCII / hyphenated; framework names like
  "Danish DPA" or "ISO 27701" are sanitized via `_collection_name()`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..logging_gateway import gateway
from .embeddings import Embedder, SentenceTransformerEmbedder


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collection_name(framework: str) -> str:
    """Chroma collection name: lowercase, alnum + underscore, 'framework_' prefix."""
    safe = re.sub(r"[^a-z0-9]+", "_", framework.lower()).strip("_")
    return f"framework_{safe}"


def default_embedder() -> Embedder:
    """Production default: sentence-transformers (lazy-loaded on first embed)."""
    return SentenceTransformerEmbedder()


@dataclass
class SearchHit:
    """One result row from a semantic search."""

    framework: str
    reference: str
    category: str
    requirement: str
    body: str
    score: float  # distance-derived similarity, higher == more similar


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------


class VectorStore:
    def __init__(
        self,
        *,
        embedder: Embedder | None = None,
        persist_dir: Path | None = None,
    ) -> None:
        self._embedder: Embedder = embedder or default_embedder()
        self._persist_dir = persist_dir
        self._client = None  # lazy

    # ----- client lifecycle -----

    def _ensure_client(self):
        if self._client is None:
            # Lazy import: same reason as SentenceTransformerEmbedder. A user
            # who only ever uses SQLite-backed tools should not be forced to
            # pull in chromadb.
            import chromadb

            if self._persist_dir is None:
                self._client = chromadb.EphemeralClient()
            else:
                self._persist_dir.mkdir(parents=True, exist_ok=True)
                self._client = chromadb.PersistentClient(path=str(self._persist_dir))
        return self._client

    def _collection(self, framework: str):
        client = self._ensure_client()
        return client.get_or_create_collection(
            name=_collection_name(framework),
            # cosine matches sentence-transformers' default; smaller distance = closer.
            metadata={"hnsw:space": "cosine", "framework": framework},
        )

    # ----- writes (gateway-audited) -----

    def upsert_articles(
        self,
        framework: str,
        articles: list[dict[str, Any]],
    ) -> int:
        """
        Upsert a batch of articles into the per-framework collection. Each
        article dict must include: reference, category, requirement, body.
        Returns the number of articles written.

        Idempotent: re-upserting the same id replaces the prior vector + metadata.
        """
        if not articles:
            return 0

        with gateway.access(
            actor="rag.vector_store",
            action="write",
            resource=f"chroma:{_collection_name(framework)}",
            metadata={"framework": framework, "count": len(articles)},
        ):
            ids = [f"{_collection_name(framework)}__{a['reference']}" for a in articles]
            documents = [a["body"] for a in articles]
            metadatas = [
                {
                    "framework": framework,
                    "reference": a["reference"],
                    "category": a.get("category", ""),
                    "requirement": a.get("requirement", ""),
                }
                for a in articles
            ]
            embeddings = self._embedder.embed(documents)
            self._collection(framework).upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )
        return len(articles)

    # ----- reads (gateway-audited) -----

    def query(
        self,
        query_text: str,
        *,
        k: int = 5,
        framework: str | None = None,
    ) -> list[SearchHit]:
        """
        Semantic search. If `framework` is given, only that collection is
        queried; otherwise every known framework collection is queried and
        the top-k merged by similarity.
        """
        if k < 1:
            raise ValueError("k must be >= 1")

        with gateway.access(
            actor="rag.vector_store",
            action="query",
            resource=f"chroma:{_collection_name(framework) if framework else 'all'}",
            metadata={"framework": framework or "*", "k": k, "qlen": len(query_text)},
        ):
            qvec = self._embedder.embed([query_text])[0]

            client = self._ensure_client()
            if framework is not None:
                cols = [self._collection(framework)]
            else:
                # Query every framework collection. Chroma exposes the list
                # via list_collections(); filter to ones we created.
                cols = [
                    c
                    for c in client.list_collections()
                    if c.name.startswith("framework_")
                ]

            hits: list[SearchHit] = []
            for col in cols:
                res = col.query(query_embeddings=[qvec], n_results=k)
                if not res["ids"] or not res["ids"][0]:
                    continue
                for i in range(len(res["ids"][0])):
                    meta = res["metadatas"][0][i] or {}
                    dist = (res["distances"][0][i]
                            if res.get("distances") and res["distances"][0]
                            else 1.0)
                    # cosine distance in [0, 2]; convert to a [-1, 1] similarity
                    # then clamp to [0, 1] for a friendlier "higher = better" score.
                    similarity = max(0.0, 1.0 - float(dist))
                    hits.append(
                        SearchHit(
                            framework=str(meta.get("framework", "")),
                            reference=str(meta.get("reference", "")),
                            category=str(meta.get("category", "")),
                            requirement=str(meta.get("requirement", "")),
                            body=str(res["documents"][0][i]),
                            score=similarity,
                        )
                    )

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]
