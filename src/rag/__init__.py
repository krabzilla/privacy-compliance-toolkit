"""
RAG (retrieval-augmented generation) layer -- v1.1+.

Two concerns separated:
  * embeddings  -- turn text into vectors. SentenceTransformerEmbedder for
                   production, FakeEmbedder for offline / deterministic tests.
  * vector_store -- ChromaDB wrapper, one collection per framework, every
                    read and write routed through the logging gateway so the
                    audit-before-access discipline that protects SQLite also
                    protects the new vector store.

Neither module imports its heavy backend at module load (lazy imports inside
methods) so the test suite can run without sentence-transformers installed.
"""
from __future__ import annotations

from .embeddings import Embedder, FakeEmbedder, SentenceTransformerEmbedder
from .vector_store import VectorStore

__all__ = [
    "Embedder",
    "FakeEmbedder",
    "SentenceTransformerEmbedder",
    "VectorStore",
]
