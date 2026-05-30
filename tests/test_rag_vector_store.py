"""VectorStore tests -- in-memory Chroma + FakeEmbedder; gateway-audited."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.rag.embeddings import FakeEmbedder
from src.rag.vector_store import VectorStore, _collection_name


pytest.importorskip("chromadb")


# Helper -- a few mock articles so tests are self-contained
def _mock(reference: str, body: str) -> dict:
    return {
        "reference": reference,
        "category": "Test",
        "requirement": reference + " requirement",
        "body": body,
    }


def _vs() -> VectorStore:
    return VectorStore(embedder=FakeEmbedder(dim=64), persist_dir=None)


def test_collection_name_sanitisation() -> None:
    assert _collection_name("GDPR") == "framework_gdpr"
    assert _collection_name("Danish DPA") == "framework_danish_dpa"
    assert _collection_name("ISO 27701") == "framework_iso_27701"
    assert _collection_name("NIST CSF") == "framework_nist_csf"


def test_upsert_round_trip_via_identity_query(isolated_env: Path) -> None:
    vs = _vs()
    body = "The controller shall implement appropriate technical and organisational measures."
    n = vs.upsert_articles("TestFW", [_mock("TEST § 1", body)])
    assert n == 1

    # FakeEmbedder is deterministic on text, so the query == body must rank
    # the inserted doc at score 1.0.
    hits = vs.query(body, k=3, framework="TestFW")
    assert hits and hits[0].reference == "TEST § 1"
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)


def test_query_returns_k_results(isolated_env: Path) -> None:
    vs = _vs()
    vs.upsert_articles(
        "TestFW",
        [_mock(f"TEST § {i}", f"body for article {i}") for i in range(10)],
    )
    hits = vs.query("body for article 3", k=5, framework="TestFW")
    assert len(hits) == 5
    # Identity match ranks first.
    assert hits[0].reference == "TEST § 3"


def test_upsert_is_idempotent(isolated_env: Path) -> None:
    vs = _vs()
    articles = [_mock(f"TEST § {i}", f"body {i}") for i in range(3)]
    vs.upsert_articles("TestFW", articles)
    vs.upsert_articles("TestFW", articles)
    # Re-upsert replaces, doesn't duplicate -- a query for one body should
    # find exactly one identity match, not two.
    hits = vs.query("body 1", k=10, framework="TestFW")
    top_matches = [h for h in hits if h.reference == "TEST § 1"]
    assert len(top_matches) == 1


def test_query_across_all_frameworks(isolated_env: Path) -> None:
    vs = _vs()
    vs.upsert_articles("FW1", [_mock("FW1 § 1", "first framework content")])
    vs.upsert_articles("FW2", [_mock("FW2 § 1", "second framework content")])
    hits = vs.query("first framework content", k=5)  # no framework=
    assert any(h.framework == "FW1" and h.reference == "FW1 § 1" for h in hits)


def test_query_scoped_to_one_framework_excludes_others(isolated_env: Path) -> None:
    vs = _vs()
    vs.upsert_articles("FW1", [_mock("FW1 § 1", "framework one body")])
    vs.upsert_articles("FW2", [_mock("FW2 § 1", "framework two body")])
    hits = vs.query("framework one body", k=5, framework="FW1")
    assert all(h.framework == "FW1" for h in hits)


def test_query_rejects_zero_k(isolated_env: Path) -> None:
    vs = _vs()
    with pytest.raises(ValueError):
        vs.query("anything", k=0)


def test_upsert_empty_is_noop(isolated_env: Path) -> None:
    vs = _vs()
    assert vs.upsert_articles("TestFW", []) == 0


def test_gateway_audits_write_and_query(isolated_env: Path) -> None:
    """Every read/write must produce an audit row through the gateway."""
    from src.logging_gateway import gateway

    vs = _vs()
    vs.upsert_articles("TestFW", [_mock("TEST § 1", "hello")])
    vs.query("hello", k=1, framework="TestFW")

    with gateway.access(actor="test", action="read", resource="audit:check") as ctx:
        rows = ctx.fetch_all(
            "SELECT action, resource FROM audit_log WHERE actor = 'rag.vector_store' ORDER BY id"
        )
    actions = [r["action"] for r in rows]
    assert "write" in actions
    assert "query" in actions
