"""Notice analyzer tests -- end-to-end with deterministic fakes (no DB needed)."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("yaml")


def _req(rid, requirement, applies_when="always", category="mandatory",
         articles=("13",), title="t", question="q?", fix="add it"):
    from src.checklist import NoticeRequirement
    return NoticeRequirement(
        id=rid, title=title, gdpr_articles=tuple(articles), category=category,
        applies_when=applies_when, requirement=requirement,
        verifier_question=question, positive_indicators=(), fix=fix,
    )


def _checklist(requirements):
    from src.checklist import Checklist
    return Checklist(
        meta={"framework": "GDPR notice", "jurisdiction": "EU/EEA"},
        condition_vocabulary=frozenset({"always", "transfers_outside_eea",
                                        "legal_basis_includes_consent"}),
        requirements=tuple(requirements),
    )


def _verifier_json(status, severity="high", confidence=0.9, evidence="",
                   reasoning="r", remediation="fix"):
    return json.dumps({
        "status": status, "severity": severity, "confidence": confidence,
        "evidence_excerpt": evidence, "reasoning": reasoning,
        "suggested_remediation": remediation,
    })


def test_empty_policy_is_refused() -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.notice_analysis import NoticeAnalysisRefusal, analyze_notice

    cl = _checklist([_req("A", "some requirement text")])
    with pytest.raises(NoticeAnalysisRefusal, match="empty policy"):
        analyze_notice("   ", embedder=FakeEmbedder(dim=32),
                       llm_client=FakeLLMClient(responses=[Response(text="{}")]),
                       checklist=cl)


def test_no_applicable_requirements_is_refused() -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.notice_analysis import NoticeAnalysisRefusal, analyze_notice

    # Only a conditional requirement, and the fact is not declared -> nothing applies.
    cl = _checklist([_req("A", "text", applies_when="transfers_outside_eea")])
    with pytest.raises(NoticeAnalysisRefusal, match="no applicable requirements"):
        analyze_notice("A real sentence here. Another one.",
                       embedder=FakeEmbedder(dim=32),
                       llm_client=FakeLLMClient(responses=[Response(text="{}")]),
                       checklist=cl, org_profile=set())


def test_identity_policy_is_covered_without_llm() -> None:
    """FakeEmbedder is identity-deterministic: when a policy chunk equals the
    requirement description, cosine == 1.0 -> covered, no LLM call."""
    from src.llm import FakeLLMClient
    from src.rag import FakeEmbedder
    from src.rag.notice_analysis import analyze_notice

    body = "Processing requires a lawful basis under one of six grounds."
    cl = _checklist([_req("LAWFUL", body)])
    fc = FakeLLMClient(responses=[])  # crashes if the LLM is called
    report = analyze_notice(body, embedder=FakeEmbedder(dim=32),
                            llm_client=fc, checklist=cl)
    assert report.n_requirements == 1
    assert report.n_covered == 1
    assert report.n_gap == 0
    assert report.n_llm_verifications == 0
    assert report.findings[0].status == "covered"


def test_obvious_gap_without_llm_uses_category_severity() -> None:
    from src.llm import FakeLLMClient
    from src.rag import FakeEmbedder
    from src.rag.notice_analysis import analyze_notice

    cl = _checklist([_req("RETENTION", "How long personal data is stored.",
                          category="mandatory")])
    policy = "We sell shoes online. Free shipping over fifty euros."
    fc = FakeLLMClient(responses=[])
    report = analyze_notice(policy, embedder=FakeEmbedder(dim=32),
                            llm_client=fc, checklist=cl,
                            verify_top_gaps=0, verify_limit=0)
    assert report.n_gap == 1
    f = report.findings[0]
    assert f.status == "gap"
    assert f.severity == "high"          # mandatory -> high
    assert f.suggested_remediation == "add it"   # uses the checklist `fix`


def test_not_applicable_is_reported_not_graded() -> None:
    from src.llm import FakeLLMClient
    from src.rag import FakeEmbedder
    from src.rag.notice_analysis import analyze_notice

    cl = _checklist([
        _req("ALWAYS", "Identity of the controller and contact details."),
        _req("XFER", "International transfer safeguards.",
             applies_when="transfers_outside_eea"),
    ])
    fc = FakeLLMClient(responses=[])
    report = analyze_notice("We are Acme Ltd, contact privacy@acme.example.",
                            embedder=FakeEmbedder(dim=32), llm_client=fc,
                            checklist=cl, org_profile=set(),
                            verify_top_gaps=0, verify_limit=0)
    assert report.n_requirements == 1        # only the always-on one graded
    assert report.n_not_applicable == 1
    na = [f for f in report.findings if f.status == "not_applicable"]
    assert na and na[0].id == "XFER"


def test_borderline_is_llm_verified_on_relevant_chunks() -> None:
    """An ambiguous requirement is sent to the LLM, and the prompt it receives
    contains policy text (the relevant chunks), proving chunk-windowing wired up."""
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.notice_analysis import analyze_notice

    cl = _checklist([_req("RIGHTS", "Data subject rights and how to exercise them.")])
    policy = "We respect your rights. Contact us to exercise them."
    fc = FakeLLMClient(responses=[Response(text=_verifier_json("partial", severity="medium"))])
    # Force the single requirement into the LLM queue regardless of similarity.
    report = analyze_notice(policy, embedder=FakeEmbedder(dim=32), llm_client=fc,
                            checklist=cl, coverage_high=2.0, coverage_low=-1.0)
    assert report.n_llm_verifications == 1
    assert report.findings[0].status == "partial"
    # the prompt the fake captured must carry the policy excerpts, not be empty
    assert "rights" in fc.calls[0].lower()


def test_llm_failure_flags_for_review_not_crash() -> None:
    from src.llm import FakeLLMClient, Response
    from src.rag import FakeEmbedder
    from src.rag.notice_analysis import analyze_notice

    cl = _checklist([_req("RIGHTS", "Data subject rights description.")])
    # Non-JSON canned response -> complete_json raises LLMError inside analyze.
    fc = FakeLLMClient(responses=[Response(text="not json at all")])
    report = analyze_notice("Some policy text. More text here.",
                            embedder=FakeEmbedder(dim=32), llm_client=fc,
                            checklist=cl, coverage_high=2.0, coverage_low=-1.0)
    f = report.findings[0]
    assert f.status == "partial"
    assert "human review" in f.reasoning.lower()
