"""Gap analysis tests -- chunker, coverage maths, full analyze() with fakes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


pytest.importorskip("chromadb")


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


def test_chunker_handles_empty() -> None:
    from src.rag.gap_analysis import chunk_policy
    assert chunk_policy("") == []
    assert chunk_policy("   \n\n ") == []


def test_chunker_groups_sentences() -> None:
    from src.rag.gap_analysis import chunk_policy
    text = "One. Two. Three. Four. Five. Six. Seven. Eight."
    chunks = chunk_policy(text, sentences_per_chunk=3)
    assert len(chunks) == 3
    assert chunks[0].startswith("One.")
    assert chunks[2].startswith("Seven.")


def test_chunker_normalises_whitespace() -> None:
    from src.rag.gap_analysis import chunk_policy
    chunks = chunk_policy("Sentence one.\n\n\tSentence two.")
    assert len(chunks) == 1
    assert "  " not in chunks[0]


# ---------------------------------------------------------------------------
# Cosine + coverage math
# ---------------------------------------------------------------------------


def test_cosine_identical_is_one() -> None:
    from src.rag.gap_analysis import _cosine
    assert _cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero() -> None:
    from src.rag.gap_analysis import _cosine
    assert _cosine([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]) == pytest.approx(0.0)


def test_score_coverage_picks_max() -> None:
    from src.rag.gap_analysis import score_coverage
    a = [1.0, 0.0]
    chunks = [[0.0, 1.0], [0.5, 0.5], [1.0, 0.0]]  # the third is identical
    assert score_coverage(a, chunks) == pytest.approx(1.0)


def test_score_coverage_empty_chunks_is_zero() -> None:
    from src.rag.gap_analysis import score_coverage
    assert score_coverage([1.0], []) == 0.0


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _insert_articles(framework: str, articles: list[dict]) -> None:
    """Insert framework + articles into SQLite for the analyzer to load."""
    from src.logging_gateway import gateway

    with gateway.access(
        actor="test.setup", action="write", resource="frameworks:setup"
    ) as ctx:
        fid = ctx.execute(
            "INSERT INTO frameworks (name, version, source, source_hash) VALUES (?, ?, ?, ?)",
            (framework, "test", "test://", "h"),
        )
        for a in articles:
            ctx.execute(
                "INSERT INTO articles (framework_id, category, requirement, body, reference, body_hash)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (fid, a.get("category", "c"), a["requirement"], a["body"], a["reference"], "h"),
            )


def _verifier_response(status: str, severity: str = "medium",
                       confidence: float = 0.8,
                       evidence: str = "",
                       reasoning: str = "test reasoning",
                       remediation: str = "test remediation") -> str:
    """Build a valid JSON string a fake LLM would return for one verification."""
    return json.dumps({
        "status": status,
        "severity": severity,
        "confidence": confidence,
        "evidence_excerpt": evidence,
        "reasoning": reasoning,
        "suggested_remediation": remediation,
    })


# ---------------------------------------------------------------------------
# analyze() -- end-to-end with fakes
# ---------------------------------------------------------------------------


def test_empty_policy_is_refused(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.gap_analysis import GapAnalysisRefusal, analyze

    _insert_articles("FW", [{"reference": "FW § 1", "requirement": "r", "body": "b"}])
    with pytest.raises(GapAnalysisRefusal, match="empty policy"):
        analyze("   ", embedder=FakeEmbedder(dim=32),
                llm_client=FakeLLMClient(responses=[Response(text="{}")]),
                framework="FW")


def test_null_byte_in_policy_is_refused(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.gap_analysis import GapAnalysisRefusal, analyze

    _insert_articles("FW", [{"reference": "FW § 1", "requirement": "r", "body": "b"}])
    with pytest.raises(GapAnalysisRefusal, match="policy text rejected"):
        analyze("policy \x00 here", embedder=FakeEmbedder(dim=32),
                llm_client=FakeLLMClient(responses=[Response(text="{}")]),
                framework="FW")


def test_unknown_framework_is_refused(isolated_env: Path) -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.gap_analysis import GapAnalysisRefusal, analyze

    with pytest.raises(GapAnalysisRefusal, match="no articles loaded"):
        analyze("Policy text. Another sentence.",
                embedder=FakeEmbedder(dim=32),
                llm_client=FakeLLMClient(responses=[Response(text="{}")]),
                framework="NotLoaded")


def test_identity_policy_is_marked_covered(isolated_env: Path) -> None:
    """When the policy IS the article body, semantic coverage should score
    high and the analyzer should mark the article 'covered' without ever
    needing to call the LLM (FakeEmbedder is identity-deterministic)."""
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.gap_analysis import analyze

    body = "Processing requires a lawful basis under one of six grounds."
    _insert_articles("FW", [{"reference": "FW § 1", "requirement": "Lawful basis", "body": body}])
    # If LLM is called we crash on exhaustion -- this asserts no LLM call.
    fc = FakeLLMClient(responses=[])
    report = analyze(body, embedder=FakeEmbedder(dim=32),
                     llm_client=fc, framework="FW",
                     coverage_high=0.55, coverage_low=0.30)
    assert report.n_articles == 1
    assert report.n_covered == 1
    assert report.n_gap == 0
    assert report.n_llm_verifications == 0
    assert report.findings[0].status == "covered"


def test_obvious_gap_is_marked_gap(isolated_env: Path) -> None:
    """Policy completely unrelated to the article: semantic similarity is
    near zero, so the article goes to 'probably_gap'. With verify_top_gaps=0
    we never reach the LLM; with > 0 the LLM is consulted on top-N."""
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.gap_analysis import analyze

    _insert_articles("FW", [{
        "reference": "FW § 1",
        "requirement": "Records of processing",
        "body": "Controllers shall maintain a record of processing activities under their responsibility.",
    }])
    policy = "We collect cookies for analytics. Cookies last 30 days."
    fc = FakeLLMClient(responses=[])  # we'll cap verify_top_gaps to 0 so no LLM
    report = analyze(policy, embedder=FakeEmbedder(dim=32),
                     llm_client=fc, framework="FW",
                     verify_top_gaps=0, verify_limit=0)
    assert report.n_articles == 1
    assert report.n_gap == 1
    assert report.findings[0].status == "gap"
    assert "max chunk similarity" in report.findings[0].reasoning


def test_llm_drift_marks_finding_as_needs_review(isolated_env: Path) -> None:
    """If the LLM returns garbage JSON during verification of a borderline
    article, the analyzer should not crash -- it should mark that finding
    as needing human review and continue building the report."""
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.gap_analysis import analyze

    _insert_articles("FW", [{
        "reference": "FW § 1",
        "requirement": "DPO",
        "body": "Controllers shall designate a Data Protection Officer.",
    }])
    # Force ambiguity by setting coverage_high HIGH and coverage_low LOW.
    # That puts even mid-similarity articles into ambiguous.
    fc = FakeLLMClient(responses=[Response(text="this is not json {{{")])
    report = analyze("Generic policy about data and cookies.",
                     embedder=FakeEmbedder(dim=32),
                     llm_client=fc, framework="FW",
                     coverage_high=0.99, coverage_low=0.01)
    # complete_json on the bad text raises LLMError; analyze() catches it
    # per-finding and marks the article as needs-review -- exactly what we
    # want: one bad LLM call does not poison the whole report.
    assert report.findings[0].status == "partial"
    assert "LLM verification failed" in report.findings[0].reasoning


def test_verifier_response_validates_schema(isolated_env: Path) -> None:
    """The verifier's parsed dict must match the schema (status in
    {covered,partial,gap}, valid severity, confidence in [0,1])."""
    from src.rag.gap_analysis import _parse_verifier_response
    from src.llm.client import LLMError

    # Happy path -- dict input now (post v1.3 fix).
    parsed = _parse_verifier_response(
        {"status": "covered", "severity": "low", "confidence": 0.9,
         "evidence_excerpt": "", "reasoning": "r", "suggested_remediation": ""}
    )
    assert parsed["status"] == "covered"

    # Drift cases -- still raise LLMError on contract violations.
    for bad in [
        {"status": "uncertain"},                       # bad status enum
        {"status": "covered", "severity": "x"},        # bad severity enum
        {"status": "covered", "confidence": 1.5},     # out of range
        "not a dict",                                  # wrong type entirely
        [],                                            # not a dict
    ]:
        with pytest.raises(LLMError):
            _parse_verifier_response(bad)


def test_finding_evidence_pii_is_redacted(isolated_env: Path) -> None:
    """If the LLM quotes back PII from the policy in the evidence excerpt,
    the analyzer must redact it before returning."""
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.gap_analysis import analyze

    _insert_articles("FW", [{
        "reference": "FW § 1",
        "requirement": "Contact",
        "body": "Provide a contact for data subject requests.",
    }])
    leaky = _verifier_response("covered",
                               evidence="contact alice@example.com for requests")
    fc = FakeLLMClient(responses=[Response(text=leaky)])
    report = analyze("Some policy text. Contact alice@example.com please.",
                     embedder=FakeEmbedder(dim=32),
                     llm_client=fc, framework="FW",
                     coverage_high=0.99, coverage_low=0.01)
    # alice@example.com should have been redacted out of the evidence excerpt.
    assert "alice@example.com" not in report.findings[0].evidence
    assert "[PII:EMAIL]" in report.findings[0].evidence


def test_per_framework_summary_buckets_correctly(isolated_env: Path) -> None:
    """Smoke test of the overall counts when the analyzer produces mixed
    statuses."""
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.gap_analysis import analyze

    body = "Processing requires a lawful basis under one of six grounds."
    _insert_articles("FW", [
        {"reference": "FW § 1", "requirement": "covered one", "body": body},
        {"reference": "FW § 2", "requirement": "obvious gap",
         "body": "Mandatory color theme of the privacy banner shall be teal."},
    ])
    fc = FakeLLMClient(responses=[])
    report = analyze(body, embedder=FakeEmbedder(dim=32),
                     llm_client=fc, framework="FW",
                     verify_top_gaps=0, verify_limit=0)
    assert report.n_articles == 2
    assert report.n_covered + report.n_gap == 2
    assert report.n_llm_verifications == 0
