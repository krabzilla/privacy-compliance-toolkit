"""ask_compliance MCP tool test -- inject both FakeLLMClient and FakeEmbedder."""
from __future__ import annotations

from pathlib import Path

import pytest


pytest.importorskip("chromadb")


def _seed_setup(isolated_env: Path):
    """Set up a fresh VectorStore + matching DB rows for one framework."""
    from src.logging_gateway import gateway
    from src.rag import FakeEmbedder, VectorStore

    fw = "TestFW"
    arts = [
        {"reference": "TEST § 1", "category": "Lawfulness", "requirement": "Lawful basis",
         "body": "Processing requires a lawful basis under one of six grounds."},
        {"reference": "TEST § 2", "category": "Consent", "requirement": "Consent conditions",
         "body": "Where processing is based on consent, the controller must demonstrate it."},
    ]
    # Insert into SQLite so the engine's known-references check passes.
    with gateway.access(actor="test.setup", action="write", resource="frameworks:setup") as ctx:
        fid = ctx.execute(
            "INSERT INTO frameworks (name, version, source, source_hash) VALUES (?, ?, ?, ?)",
            (fw, "test", "test://", "h"),
        )
        for a in arts:
            ctx.execute(
                "INSERT INTO articles (framework_id, category, requirement, body, reference, body_hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (fid, a["category"], a["requirement"], a["body"], a["reference"], "h"),
            )
    # Index into Chroma.
    vs = VectorStore(embedder=FakeEmbedder(dim=64), persist_dir=None)
    vs.upsert_articles(fw, arts)
    return fw, vs


def test_happy_path_via_mcp_tool(isolated_env: Path) -> None:
    from src.llm import Citation, FakeLLMClient, Response
    from src.mcp_server import server as srv

    fw, vs = _seed_setup(isolated_env)
    resp = Response(
        text="Lawful basis means one of six grounds in TEST § 1.",
        citations=[Citation(framework=fw, reference="TEST § 1")],
        confidence=0.9,
    )
    srv.set_vector_store(vs)
    srv.set_llm_client(FakeLLMClient(responses=[resp]))
    try:
        out = srv.ask_compliance("What is the lawful basis?", framework=fw)
        assert out["ok"] is True
        assert out["citations"] == [{"framework": fw, "reference": "TEST § 1"}]
        assert out["confidence"] == 0.9
        assert "Lawful basis" in out["answer"]
        assert "TEST § 1" in out["retrieved_refs"]
    finally:
        srv.set_vector_store(None)
        srv.set_llm_client(None)


def test_hallucinated_citation_returns_refusal_envelope(isolated_env: Path) -> None:
    from src.llm import Citation, FakeLLMClient, Response
    from src.mcp_server import server as srv

    fw, vs = _seed_setup(isolated_env)
    resp = Response(
        text="See TEST § 99.",
        citations=[Citation(framework=fw, reference="TEST § 99")],
        confidence=0.95,
    )
    srv.set_vector_store(vs)
    srv.set_llm_client(FakeLLMClient(responses=[resp]))
    try:
        out = srv.ask_compliance("anything", framework=fw)
        assert out["ok"] is False
        assert out["error"] == "guardrail_violation"
        assert "not in retrieved" in out["reason"]
    finally:
        srv.set_vector_store(None)
        srv.set_llm_client(None)


def test_null_byte_in_question_returns_refusal(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, Response
    from src.mcp_server import server as srv

    fw, vs = _seed_setup(isolated_env)
    srv.set_vector_store(vs)
    srv.set_llm_client(FakeLLMClient(responses=[Response(text="x", confidence=0.9)]))
    try:
        out = srv.ask_compliance("query \x00 here", framework=fw)
        assert out["ok"] is False
        assert out["error"] == "guardrail_violation"
    finally:
        srv.set_vector_store(None)
        srv.set_llm_client(None)


def test_low_confidence_returns_refusal(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, Response
    from src.mcp_server import server as srv

    fw, vs = _seed_setup(isolated_env)
    resp = Response(text="dunno", citations=[], confidence=0.3)
    srv.set_vector_store(vs)
    srv.set_llm_client(FakeLLMClient(responses=[resp]))
    try:
        out = srv.ask_compliance("anything", framework=fw)
        assert out["ok"] is False
        assert "confidence" in out["reason"]
    finally:
        srv.set_vector_store(None)
        srv.set_llm_client(None)


def test_pii_in_answer_is_redacted(isolated_env: Path) -> None:
    from src.llm import Citation, FakeLLMClient, Response
    from src.mcp_server import server as srv

    fw, vs = _seed_setup(isolated_env)
    resp = Response(
        text="Contact alice@example.com for more on TEST § 1.",
        citations=[Citation(framework=fw, reference="TEST § 1")],
        confidence=0.9,
    )
    srv.set_vector_store(vs)
    srv.set_llm_client(FakeLLMClient(responses=[resp]))
    try:
        out = srv.ask_compliance("anything", framework=fw)
        assert out["ok"] is True
        assert "alice@example.com" not in out["answer"]
        assert "[PII:EMAIL]" in out["answer"]
    finally:
        srv.set_vector_store(None)
        srv.set_llm_client(None)
