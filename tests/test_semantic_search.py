"""semantic_search MCP tool test -- injects a FakeEmbedder-backed store."""
from __future__ import annotations

from pathlib import Path

import pytest


pytest.importorskip("chromadb")


def _seed_vs():
    """Build an in-memory VectorStore seeded with three mock articles."""
    from src.rag import FakeEmbedder, VectorStore

    vs = VectorStore(embedder=FakeEmbedder(dim=64), persist_dir=None)
    vs.upsert_articles(
        "GDPR",
        [
            {
                "reference": "GDPR Art. 6",
                "category": "Lawfulness",
                "requirement": "Lawful basis",
                "body": "Processing requires a lawful basis under one of six grounds.",
            },
            {
                "reference": "GDPR Art. 7",
                "category": "Consent",
                "requirement": "Conditions for consent",
                "body": "Where processing is based on consent, the controller must demonstrate it.",
            },
            {
                "reference": "GDPR Art. 32",
                "category": "Security",
                "requirement": "Security of processing",
                "body": "Implement appropriate technical and organisational measures.",
            },
        ],
    )
    return vs


def test_identity_query_returns_top_match(isolated_env: Path) -> None:
    from src.mcp_server import server as srv

    srv.set_vector_store(_seed_vs())
    try:
        out = srv.semantic_search(
            "Processing requires a lawful basis under one of six grounds.",
            k=3,
            framework="GDPR",
        )
        assert out["ok"] is True
        assert out["count"] >= 1
        assert out["results"][0]["reference"] == "GDPR Art. 6"
        assert out["results"][0]["score"] == pytest.approx(1.0, abs=1e-4)
    finally:
        srv.set_vector_store(None)


def test_k_is_clamped(isolated_env: Path) -> None:
    from src.mcp_server import server as srv

    srv.set_vector_store(_seed_vs())
    try:
        # Asking for 999 must clamp to <= 20 (the configured upper bound)
        # AND respect "we only have 3 docs" so count <= 3.
        out = srv.semantic_search("any query", k=999, framework="GDPR")
        assert out["ok"] is True
        assert out["count"] <= 3
    finally:
        srv.set_vector_store(None)


def test_input_sanitisation_refuses_null_byte(isolated_env: Path) -> None:
    from src.mcp_server import server as srv

    srv.set_vector_store(_seed_vs())
    try:
        out = srv.semantic_search("query with \x00 null", k=5)
        # Refusal envelope; never returns hits for a guardrail violation.
        assert out["ok"] is False
        assert out["error"] == "guardrail_violation"
    finally:
        srv.set_vector_store(None)


def test_pii_in_body_is_redacted_in_snippet(isolated_env: Path) -> None:
    """Articles loaded with PII in the body should have it redacted in the
    returned snippet (output guardrails run on every tool response)."""
    from src.rag import FakeEmbedder, VectorStore
    from src.mcp_server import server as srv

    vs = VectorStore(embedder=FakeEmbedder(dim=64), persist_dir=None)
    leaky_body = "Contact alice@example.com regarding processing requirements."
    vs.upsert_articles(
        "GDPR",
        [
            {
                "reference": "GDPR Art. 6",
                "category": "Lawfulness",
                "requirement": "Lawful basis",
                "body": leaky_body,
            }
        ],
    )
    srv.set_vector_store(vs)
    try:
        out = srv.semantic_search(leaky_body, k=1, framework="GDPR")
        assert out["ok"] is True
        snip = out["results"][0]["snippet"]
        assert "alice@example.com" not in snip
        assert "[PII:EMAIL]" in snip
    finally:
        srv.set_vector_store(None)


def test_scope_to_framework_excludes_others(isolated_env: Path) -> None:
    from src.rag import FakeEmbedder, VectorStore
    from src.mcp_server import server as srv

    vs = VectorStore(embedder=FakeEmbedder(dim=64), persist_dir=None)
    vs.upsert_articles("GDPR", [{"reference": "GDPR Art. 6", "category": "X", "requirement": "Y", "body": "shared body"}])
    vs.upsert_articles("NIST CSF", [{"reference": "NIST CSF GV.OC-01", "category": "Govern", "requirement": "Context", "body": "shared body"}])
    srv.set_vector_store(vs)
    try:
        out = srv.semantic_search("shared body", k=5, framework="GDPR")
        assert out["ok"] is True
        assert all(r["framework"] == "GDPR" for r in out["results"])
    finally:
        srv.set_vector_store(None)
