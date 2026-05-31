"""RAG engine tests -- the critical 'no hallucinated citations' path.

Each test stands up its own VectorStore + unique framework name + indexes its
own mock articles via FakeEmbedder. That side-steps ChromaDB's shared-state
across in-process EphemeralClient instances (collections from earlier tests
can leak across; using unique framework names per test makes that benign).
"""
from __future__ import annotations

from pathlib import Path

import pytest


pytest.importorskip("chromadb")


def _vs_with_articles(framework: str, articles: list[dict]):
    from src.rag import FakeEmbedder, VectorStore
    vs = VectorStore(embedder=FakeEmbedder(dim=64), persist_dir=None)
    vs.upsert_articles(framework, articles)
    return vs


# The articles every test uses; each test scopes to its own framework name
# so retrieval is deterministic regardless of cross-test state.
def _articles() -> list[dict]:
    return [
        {"reference": "TEST § 1", "category": "Lawfulness", "requirement": "Lawful basis",
         "body": "Processing requires a lawful basis under one of six grounds."},
        {"reference": "TEST § 2", "category": "Consent", "requirement": "Consent conditions",
         "body": "Where processing is based on consent, the controller must demonstrate it."},
        {"reference": "TEST § 3", "category": "Security", "requirement": "Security of processing",
         "body": "Implement appropriate technical and organisational measures."},
    ]


def _load_one_test_article(isolated_env: Path, framework: str, reference: str, body: str) -> None:
    """Insert a single row into the SQLite articles table so the engine's
    _all_known_references() check sees the reference as known."""
    from src.logging_gateway import gateway

    with gateway.access(actor="test.setup", action="write", resource="frameworks:setup") as ctx:
        fid = ctx.execute(
            "INSERT INTO frameworks (name, version, source, source_hash) VALUES (?, ?, ?, ?)",
            (framework, "test", "test://" + framework, "h"),
        )
        ctx.execute(
            "INSERT INTO articles (framework_id, category, requirement, body, reference, body_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (fid, "cat", "req", body, reference, "h"),
        )


def _load_test_articles(isolated_env: Path, framework: str, articles: list[dict]) -> None:
    from src.logging_gateway import gateway

    with gateway.access(actor="test.setup", action="write", resource="frameworks:setup") as ctx:
        fid = ctx.execute(
            "INSERT INTO frameworks (name, version, source, source_hash) VALUES (?, ?, ?, ?)",
            (framework, "test", "test://" + framework, "h"),
        )
        for a in articles:
            ctx.execute(
                "INSERT INTO articles (framework_id, category, requirement, body, reference, body_hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (fid, a.get("category", "c"), a.get("requirement", "r"),
                 a["body"], a["reference"], "h"),
            )


def test_happy_path_returns_answer_with_verified_citations(isolated_env: Path) -> None:
    from src.llm import Citation, FakeLLMClient, Response
    from src.rag.engine import answer

    fw = "HappyPath"
    arts = _articles()
    _load_test_articles(isolated_env, fw, arts)
    vs = _vs_with_articles(fw, arts)

    resp = Response(
        text="The lawful basis for processing is one of the six grounds in TEST § 1.",
        citations=[Citation(framework=fw, reference="TEST § 1")],
        confidence=0.9,
    )
    fc = FakeLLMClient(responses=[resp])

    a = answer("What is the lawful basis?", vector_store=vs, llm_client=fc, framework=fw)
    assert a.text.startswith("The lawful basis")
    assert a.citations == [Citation(framework=fw, reference="TEST § 1")]
    assert a.confidence == 0.9


def test_hallucinated_citation_not_in_retrieved_is_refused(isolated_env: Path) -> None:
    """The v1.2 thesis test: model returns a fabricated reference -> RAGRefusal."""
    from src.llm import Citation, FakeLLMClient, Response
    from src.rag.engine import RAGRefusal, answer

    fw = "Hallucination1"
    arts = _articles()
    _load_test_articles(isolated_env, fw, arts)
    vs = _vs_with_articles(fw, arts)

    resp = Response(
        text="See TEST § 99.",
        citations=[Citation(framework=fw, reference="TEST § 99")],  # not retrieved, not real
        confidence=0.95,
    )
    fc = FakeLLMClient(responses=[resp])

    with pytest.raises(RAGRefusal, match="not in retrieved"):
        answer("anything", vector_store=vs, llm_client=fc, framework=fw)


def test_real_citation_not_shown_to_model_is_refused(isolated_env: Path) -> None:
    """Model cites a real article that was NOT in the retrieved set -> refused.
    This catches "remembered from training" hallucinations."""
    from src.llm import Citation, FakeLLMClient, Response
    from src.rag.engine import RAGRefusal, answer

    fw = "Hallucination2"
    arts = _articles()
    _load_test_articles(isolated_env, fw, arts)
    # ALSO insert TEST § 4 as a known real article, but DO NOT index it in
    # the vector store -- so retrieval never returns it, but the known-refs
    # set does contain it.
    _load_one_test_article(isolated_env, fw + "_extra", "TEST § 4", "extra body")
    vs = _vs_with_articles(fw, arts)   # vs only has §§ 1-3

    resp = Response(
        text="Per TEST § 4 ...",
        citations=[Citation(framework=fw, reference="TEST § 4")],
        confidence=0.95,
    )
    fc = FakeLLMClient(responses=[resp])

    with pytest.raises(RAGRefusal, match="not in retrieved"):
        answer("anything", vector_store=vs, llm_client=fc, framework=fw)


def test_low_confidence_is_refused(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag.engine import RAGRefusal, answer

    fw = "LowConf"
    arts = _articles()
    _load_test_articles(isolated_env, fw, arts)
    vs = _vs_with_articles(fw, arts)

    resp = Response(text="not sure", citations=[], confidence=0.3)
    fc = FakeLLMClient(responses=[resp])

    with pytest.raises(RAGRefusal, match="confidence"):
        answer("anything", vector_store=vs, llm_client=fc, framework=fw)


def test_llm_transport_failure_is_refused(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, LLMError
    from src.rag.engine import RAGRefusal, answer

    fw = "Transport"
    arts = _articles()
    _load_test_articles(isolated_env, fw, arts)
    vs = _vs_with_articles(fw, arts)

    def boom(prompt):
        raise LLMError("backend offline")

    fc = FakeLLMClient(factory=boom)

    with pytest.raises(RAGRefusal, match="LLM call failed"):
        answer("anything", vector_store=vs, llm_client=fc, framework=fw)


def test_empty_retrieval_is_refused(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag.engine import RAGRefusal, answer
    from src.rag import FakeEmbedder, VectorStore

    # Fresh vs with NO upserts, AND a unique framework so even leaked state
    # from other tests cannot satisfy retrieval.
    vs = VectorStore(embedder=FakeEmbedder(dim=64), persist_dir=None)
    fc = FakeLLMClient(responses=[Response(text="x", confidence=0.9)])

    with pytest.raises(RAGRefusal, match="no relevant rules"):
        answer("anything", vector_store=vs, llm_client=fc, framework="NeverIndexedFW")


def test_pii_in_answer_text_is_redacted(isolated_env: Path) -> None:
    from src.llm import Citation, FakeLLMClient, Response
    from src.rag.engine import answer

    fw = "PIIRedact"
    arts = _articles()
    _load_test_articles(isolated_env, fw, arts)
    vs = _vs_with_articles(fw, arts)

    resp = Response(
        text="Contact alice@example.com for details about TEST § 1.",
        citations=[Citation(framework=fw, reference="TEST § 1")],
        confidence=0.9,
    )
    fc = FakeLLMClient(responses=[resp])

    a = answer("anything", vector_store=vs, llm_client=fc, framework=fw)
    assert "alice@example.com" not in a.text
    assert "[PII:EMAIL]" in a.text


def test_empty_question_is_refused(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag.engine import RAGRefusal, answer

    fw = "EmptyQ"
    arts = _articles()
    _load_test_articles(isolated_env, fw, arts)
    vs = _vs_with_articles(fw, arts)
    fc = FakeLLMClient(responses=[Response(text="x", confidence=0.9)])

    with pytest.raises(RAGRefusal):
        answer("   ", vector_store=vs, llm_client=fc, framework=fw)


def test_null_byte_in_question_is_refused(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag.engine import RAGRefusal, answer

    fw = "NullByte"
    arts = _articles()
    _load_test_articles(isolated_env, fw, arts)
    vs = _vs_with_articles(fw, arts)
    fc = FakeLLMClient(responses=[Response(text="x", confidence=0.9)])

    with pytest.raises(RAGRefusal, match="input rejected"):
        answer("query \x00 with null", vector_store=vs, llm_client=fc, framework=fw)


def test_structured_citations_immune_to_noncanonical_phrasing(isolated_env: Path) -> None:
    """The v1.2 architectural payoff: even if the LLM's text uses non-canonical
    phrasing ('section 99 of TEST'), the structured citations field is what
    we validate. The text phrasing is irrelevant to verification. This is
    exactly the gap the four v0.1/v1.0b 'xfail(strict=True)' markers document
    for the regex-extraction path; structured emission sidesteps it."""
    from src.llm import Citation, FakeLLMClient, Response
    from src.rag.engine import answer

    fw = "Structured"
    arts = _articles()
    _load_test_articles(isolated_env, fw, arts)
    vs = _vs_with_articles(fw, arts)

    # Text contains a free-prose fake citation that the v0.1 regex would NOT
    # extract ("section 99 of TEST"). But the structured citations field is
    # a valid retrieved reference. Engine accepts because what matters is
    # the structured field, not regex extraction from text.
    resp = Response(
        text="Per section 99 of TEST and other authorities, consent must be informed.",
        citations=[Citation(framework=fw, reference="TEST § 2")],  # real + retrieved
        confidence=0.9,
    )
    fc = FakeLLMClient(responses=[resp])

    a = answer("anything", vector_store=vs, llm_client=fc, framework=fw)
    # The text passes through (PII redaction only); verification is structural.
    assert a.citations == [Citation(framework=fw, reference="TEST § 2")]
