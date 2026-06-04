"""
v1.5 -- Privacy-NOTICE gap analysis (checklist-driven).

This is the corrected successor to the v1.3 article-driven `gap_analysis`.

Why a separate path: grading a public privacy NOTICE against all 99 GDPR
articles is wrong. Most of the regulation imposes internal/operational duties
(ROPA Art. 30, cooperation Art. 31, security Art. 32, DPIA Art. 35) that never
belong in a notice and must not be scored against one -- doing so was what
produced the false "Art. 30/31/32/35 gap" noise. This analyzer scores the
policy against a curated NOTICE-requirement checklist (Arts. 12-14 + the
Danish CPR overlay) loaded from data/checklists/.

Two fixes over v1.3 live here:

  1. Right target set. We grade ~9-18 notice disclosures (filtered by the org's
     declared profile), not 99 articles. A requirement whose condition is false
     is reported N/A, never a gap.

  2. Chunk-windowed LLM verification. The v1.3 verifier shipped the ENTIRE
     policy into every prompt with a 24K-token budget, which timed out on CPU
     Mistral (every verification failed -> "flagged for human review"). Here
     each verification sees only the few policy passages most relevant to that
     requirement, so prompts are small and the call actually finishes.

Pipeline (each step fails loud -> NoticeAnalysisRefusal, never a partial answer):

  1) sanitize policy text                  input guardrail
  2) chunk + embed the policy              reuse gap_analysis.chunk_policy
  3) load checklist, filter by org_profile applicable() = always + declared facts
  4) embed each requirement's `requirement` description (NOT the title)
  5) coverage scoring                      max cosine to any policy chunk ->
                                           covered (high) / gap (low) / ambiguous
  6) LLM verification of ambiguous + top   sees only top-K relevant chunks;
     gaps, bounded by VERIFY_LIMIT         answers the requirement's yes/no
  7) assemble report                       covered / partial / gap / N/A counts

Dependency injection mirrors gap_analysis: embedder and llm_client are required
parameters. Production wires SentenceTransformerEmbedder + OllamaClient; tests
wire FakeEmbedder + FakeLLMClient.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..checklist import Checklist, ChecklistError, NoticeRequirement, load_checklist
from ..guardrails.input import GuardrailViolation, sanitize_text
from ..guardrails.output import redact_pii
from ..guardrails.processing import enforce_token_budget
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import build_notice_verification_prompt
from .embeddings import Embedder
from .gap_analysis import (
    COVERAGE_HIGH,
    COVERAGE_LOW,
    MAX_POLICY_CHARS,
    VERIFY_LIMIT,
    VERIFY_TOP_GAPS,
    _cosine,
    _parse_verifier_response,
    chunk_policy,
    score_coverage,
)

# How many of the most-relevant policy chunks to show the LLM per requirement.
# Small on purpose: this is the knob that keeps verification prompts tiny (and
# therefore fast) compared with the v1.3 whole-policy approach.
TOP_CHUNKS_PER_REQUIREMENT = 3
# Per-verification token budget. With only a handful of chunks in the prompt
# this is generous headroom, not a tight ceiling like the v1.3 24K.
NOTICE_VERIFY_TOKENS = 6_000

# Severity floor for non-LLM-verified findings, keyed off the requirement's
# checklist category. Mandatory disclosures missing are high-impact; recommended
# ones are low.
_CATEGORY_SEVERITY = {"mandatory": "high", "conditional": "medium", "recommended": "low"}


class NoticeAnalysisRefusal(GuardrailViolation):
    """The notice analyzer refused to produce a report. Reason is in the message."""


@dataclass(frozen=True)
class NoticeFinding:
    """One checklist requirement's status against the policy."""

    id: str
    title: str
    reference: str            # e.g. "GDPR Art. 13(1)(a)"
    category: str             # mandatory | conditional | recommended
    status: str               # "covered" | "partial" | "gap" | "not_applicable"
    severity: str             # "low" | "medium" | "high"
    confidence: float         # 0.0 - 1.0
    evidence: str             # short excerpt from POLICY, or ""
    reasoning: str            # one-sentence explanation
    suggested_remediation: str  # what to add/strengthen ("" if covered/N/A)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class NoticeReport:
    framework: str
    jurisdiction: str
    org_profile: list[str]
    n_requirements: int       # applicable requirements graded
    n_covered: int
    n_partial: int
    n_gap: int
    n_not_applicable: int
    n_llm_verifications: int
    findings: list[NoticeFinding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "framework": self.framework,
            "jurisdiction": self.jurisdiction,
            "org_profile": self.org_profile,
            "n_requirements": self.n_requirements,
            "n_covered": self.n_covered,
            "n_partial": self.n_partial,
            "n_gap": self.n_gap,
            "n_not_applicable": self.n_not_applicable,
            "n_llm_verifications": self.n_llm_verifications,
            "findings": [f.to_dict() for f in self.findings],
        }


def _top_chunks(req_vec: list[float], chunks: list[str],
                chunk_vecs: list[list[float]], k: int) -> str:
    """Join the k policy chunks most similar to this requirement."""
    if not chunks:
        return ""
    scored = sorted(
        range(len(chunks)), key=lambda i: _cosine(req_vec, chunk_vecs[i]), reverse=True
    )
    return "\n\n".join(chunks[i] for i in scored[:k])


def _verify_requirement(
    llm: LLMClient,
    req: NoticeRequirement,
    policy_excerpts: str,
) -> dict:
    """Single requirement-vs-policy LLM verification. Returns the parsed result."""
    prompt = build_notice_verification_prompt(
        policy_excerpts,
        req_id=req.id,
        reference=req.reference,
        requirement=req.requirement,
        verifier_question=req.verifier_question,
        positive_indicators=req.positive_indicators,
    )
    enforce_token_budget(prompt, max_tokens=NOTICE_VERIFY_TOKENS)
    response_dict = llm.complete_json(prompt)
    return _parse_verifier_response(response_dict)


def analyze_notice(
    policy_text: str,
    *,
    embedder: Embedder,
    llm_client: LLMClient,
    org_profile: set[str] | None = None,
    checklist: Checklist | None = None,
    checklist_path: str | None = None,
    coverage_high: float = COVERAGE_HIGH,
    coverage_low: float = COVERAGE_LOW,
    verify_limit: int = VERIFY_LIMIT,
    verify_top_gaps: int = VERIFY_TOP_GAPS,
    top_chunks: int = TOP_CHUNKS_PER_REQUIREMENT,
) -> NoticeReport:
    """
    Analyze `policy_text` against the notice checklist for the declared profile.

    Raises NoticeAnalysisRefusal on input rejection, checklist failure, or an
    unrecoverable transport error.

    org_profile is the set of facts the org declares (a subset of the checklist
    condition vocabulary, minus `always` which is implied). Requirements whose
    condition is not in the profile are reported as N/A, not graded.
    """
    # 1) Input guardrails
    try:
        policy_text = sanitize_text(policy_text, max_len=MAX_POLICY_CHARS)
    except GuardrailViolation as e:
        raise NoticeAnalysisRefusal(f"policy text rejected: {e}") from e
    if not policy_text.strip():
        raise NoticeAnalysisRefusal("empty policy text")

    # 2) Load + filter the checklist
    try:
        cl = checklist or load_checklist(checklist_path)
        applicable = cl.applicable(org_profile)
        not_applicable = cl.not_applicable(org_profile)
    except ChecklistError as e:
        raise NoticeAnalysisRefusal(f"checklist error: {e}") from e
    if not applicable:
        raise NoticeAnalysisRefusal("no applicable requirements for this profile")

    # 3) Chunk + embed the policy
    chunks = chunk_policy(policy_text)
    if not chunks:
        raise NoticeAnalysisRefusal("no analysable content in policy")
    chunk_vecs = embedder.embed(chunks)

    # 4) Embed each requirement's rich description (not the title)
    req_vecs = embedder.embed([r.requirement for r in applicable])

    # 5) Coverage scores + bucketing
    scores = [score_coverage(req_vecs[i], chunk_vecs) for i in range(len(applicable))]
    ambiguous_idx: list[int] = []
    probably_gap_idx: list[int] = []
    probably_covered_idx: list[int] = []
    for i, sc in enumerate(scores):
        if sc >= coverage_high:
            probably_covered_idx.append(i)
        elif sc <= coverage_low:
            probably_gap_idx.append(i)
        else:
            ambiguous_idx.append(i)

    # 6) LLM verification: every ambiguous, plus the worst top-N probably_gap,
    #    each seeing only its most-relevant chunks. Bounded by verify_limit.
    probably_gap_idx.sort(key=lambda i: scores[i])
    verify_queue = list(ambiguous_idx) + probably_gap_idx[:verify_top_gaps]
    if len(verify_queue) > verify_limit:
        verify_queue = verify_queue[:verify_limit]

    verified: dict[int, dict] = {}
    for i in verify_queue:
        excerpts = _top_chunks(req_vecs[i], chunks, chunk_vecs, top_chunks)
        try:
            verified[i] = _verify_requirement(llm_client, applicable[i], excerpts)
        except LLMError as e:
            verified[i] = {
                "status": "partial",
                "severity": "medium",
                "confidence": 0.0,
                "evidence": "",
                "reasoning": f"LLM verification failed ({e}); flagged for human review.",
                "suggested_remediation": "Manually review this requirement against the policy.",
            }

    # 7) Build findings for applicable requirements
    findings: list[NoticeFinding] = []
    for i, req in enumerate(applicable):
        sc = scores[i]
        if i in verified:
            v = verified[i]
            findings.append(NoticeFinding(
                id=req.id,
                title=req.title,
                reference=req.reference,
                category=req.category,
                status=v["status"],
                severity=v["severity"],
                confidence=v["confidence"],
                evidence=redact_pii(v["evidence"]).text if v["evidence"] else "",
                reasoning=redact_pii(v["reasoning"]).text if v["reasoning"] else "",
                suggested_remediation=(
                    redact_pii(v["suggested_remediation"]).text
                    if v["suggested_remediation"] else ""
                ),
            ))
        elif i in set(probably_covered_idx):
            findings.append(NoticeFinding(
                id=req.id,
                title=req.title,
                reference=req.reference,
                category=req.category,
                status="covered",
                severity="low",
                confidence=min(1.0, max(0.0, sc)),
                evidence="",
                reasoning=(
                    f"Policy contains semantically similar content "
                    f"(max chunk similarity {sc:.2f}). Not LLM-verified."
                ),
                suggested_remediation="",
            ))
        else:
            # probably_gap but not in the verify-top-N
            findings.append(NoticeFinding(
                id=req.id,
                title=req.title,
                reference=req.reference,
                category=req.category,
                status="gap",
                severity=_CATEGORY_SEVERITY.get(req.category, "medium"),
                confidence=min(1.0, max(0.0, 1.0 - sc)),
                evidence="",
                reasoning=(
                    f"No semantically similar content found in the policy "
                    f"(max chunk similarity {sc:.2f}). Not LLM-verified."
                ),
                suggested_remediation=req.fix or (
                    f"Add notice language addressing {req.title!r} ({req.reference})."
                ),
            ))

    # 8) Append N/A requirements (transparency: show what was filtered out)
    for req in not_applicable:
        findings.append(NoticeFinding(
            id=req.id,
            title=req.title,
            reference=req.reference,
            category=req.category,
            status="not_applicable",
            severity="low",
            confidence=1.0,
            evidence="",
            reasoning=(
                f"Condition {req.applies_when!r} is not in the declared org "
                f"profile, so this disclosure does not apply."
            ),
            suggested_remediation="",
        ))

    n_covered = sum(1 for f in findings if f.status == "covered")
    n_partial = sum(1 for f in findings if f.status == "partial")
    n_gap = sum(1 for f in findings if f.status == "gap")
    n_na = sum(1 for f in findings if f.status == "not_applicable")
    return NoticeReport(
        framework=cl.framework,
        jurisdiction=cl.jurisdiction,
        org_profile=sorted(org_profile or set()),
        n_requirements=len(applicable),
        n_covered=n_covered,
        n_partial=n_partial,
        n_gap=n_gap,
        n_not_applicable=n_na,
        n_llm_verifications=len(verified),
        findings=findings,
    )
