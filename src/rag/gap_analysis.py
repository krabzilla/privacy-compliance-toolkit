"""
v1.3 -- Privacy-policy gap analysis.

Compares a free-text privacy policy against the loaded framework articles
and reports, per article, whether the policy COVERS, partially addresses
(PARTIAL), or has a GAP for that requirement -- with severity, confidence,
evidence excerpt, reasoning, and a suggested remediation.

Pipeline (every step has an explicit failure mode -> GapAnalysisRefusal,
never a partial answer):

  1) sanitize policy text                  input guardrail
  2) chunk the policy                      ~3-5 sentence chunks; each chunk
                                           is embedded and used as the
                                           retrieval surface for coverage
  3) load framework articles + their       gateway-audited SELECT
     reference embeddings
  4) coverage scoring (fast, semantic)     for each article, max cosine
                                           similarity to any policy chunk;
                                           bucket into probably_covered (high
                                           sim), probably_gap (low sim), or
                                           ambiguous (middle)
  5) LLM verification of ambiguous +       Mistral via the LLMClient. Bounded
     top-N probably_gap items              by VERIFY_LIMIT so a single
                                           analysis can't blow out the budget
  6) assemble report                       Finding per article, plus the
                                           covered / partial / gap counts

The fast tier uses sentence-transformers embeddings only -- no LLM call --
so articles with strong textual coverage are decided in milliseconds. The
LLM tier is invoked only where similarity is inconclusive, keeping a typical
analysis under ~2 minutes on CPU Mistral.

Same dependency-injection discipline as rag.engine: vector_store, embedder
and llm_client are required parameters. The caller wires in production
defaults; tests wire in FakeEmbedder / FakeLLMClient.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

from ..guardrails.input import GuardrailViolation, sanitize_text
from ..guardrails.output import enforce_confidence, redact_pii
from ..guardrails.processing import enforce_token_budget
from ..llm.client import LLMClient, LLMError
from ..llm.prompts import build_gap_analysis_prompt
from ..logging_gateway import gateway
from .embeddings import Embedder


# ---------------------------------------------------------------------------
# Tuning knobs (override per-call if needed)
# ---------------------------------------------------------------------------

# Similarity bucketing -- both sides of the threshold are conservative on
# purpose. Anything in [LOW, HIGH] falls into the LLM-verified middle.
COVERAGE_HIGH = 0.55     # >= this -> probably covered (skip LLM)
COVERAGE_LOW = 0.30      # <= this -> probably gap (LLM still confirms top N)
# Per-analysis LLM-call budget so an analysis is bounded in time and cost.
VERIFY_LIMIT = 20
# Top-N probably_gap items to also send to the LLM for severity / remediation
# detail (rather than just labelling them "gap" with no reasoning).
VERIFY_TOP_GAPS = 8
# Policy text limits.
MAX_POLICY_CHARS = 50_000
# Chunker target -- sentences per chunk.
CHUNK_SENTENCES = 4


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class GapAnalysisRefusal(GuardrailViolation):
    """The analyzer refused to produce a report. Reason is in the message."""


@dataclass(frozen=True)
class Finding:
    """One article's status against the policy."""
    framework: str
    reference: str
    requirement: str
    status: str               # "covered" | "partial" | "gap"
    severity: str             # "low" | "medium" | "high"
    confidence: float         # 0.0 - 1.0
    evidence: str             # short excerpt from POLICY, or "" if none
    reasoning: str            # one-sentence explanation
    suggested_remediation: str  # what to add/strengthen (empty if covered)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GapReport:
    framework: str            # "GDPR" / "*" / etc. -- "*" for analyze_policy_all
    n_articles: int
    n_covered: int
    n_partial: int
    n_gap: int
    n_llm_verifications: int  # how many LLM calls this analysis spent
    findings: list[Finding] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "framework": self.framework,
            "n_articles": self.n_articles,
            "n_covered": self.n_covered,
            "n_partial": self.n_partial,
            "n_gap": self.n_gap,
            "n_llm_verifications": self.n_llm_verifications,
            "findings": [f.to_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------


_SENTENCE_SPLIT = re.compile(r"(?<=[\.!?])\s+(?=[A-Z])")


def chunk_policy(text: str, *, sentences_per_chunk: int = CHUNK_SENTENCES) -> list[str]:
    """Split a policy into overlapping-free chunks of ~N sentences each."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    chunks: list[str] = []
    for i in range(0, len(sentences), sentences_per_chunk):
        chunk = " ".join(sentences[i : i + sentences_per_chunk]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# Coverage scoring
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Returns 0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def score_coverage(
    article_vec: list[float],
    chunk_vecs: list[list[float]],
) -> float:
    """Max cosine similarity between this article and any policy chunk."""
    if not chunk_vecs:
        return 0.0
    return max(_cosine(article_vec, cv) for cv in chunk_vecs)


# ---------------------------------------------------------------------------
# Article loader (gateway-audited; one batch per framework)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Article:
    framework: str
    reference: str
    requirement: str
    body: str


def _load_articles(framework: str | None) -> list[_Article]:
    """Pull articles for one framework, or for all loaded frameworks if None."""
    sql_all = (
        "SELECT f.name AS framework, a.reference, a.requirement, a.body "
        "FROM articles a JOIN frameworks f ON f.id = a.framework_id "
        "ORDER BY f.name, a.id"
    )
    sql_one = (
        "SELECT f.name AS framework, a.reference, a.requirement, a.body "
        "FROM articles a JOIN frameworks f ON f.id = a.framework_id "
        "WHERE f.name = ? ORDER BY a.id"
    )
    resource = "articles:all" if framework is None else f"articles:{framework}"
    with gateway.access(
        actor="rag.gap_analysis",
        action="read",
        resource=resource,
    ) as ctx:
        if framework is None:
            rows = ctx.fetch_all(sql_all)
        else:
            rows = ctx.fetch_all(sql_one, (framework,))
    return [
        _Article(framework=r["framework"], reference=r["reference"],
                 requirement=r["requirement"], body=r["body"])
        for r in rows
    ]


# ---------------------------------------------------------------------------
# LLM verification
# ---------------------------------------------------------------------------


def _parse_verifier_response(raw: str) -> dict:
    """Validate the verifier's JSON response against the contract."""
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise LLMError(f"verifier response not JSON: {e}") from e
    if not isinstance(d, dict):
        raise LLMError("verifier response is not a JSON object")
    status = d.get("status")
    if status not in ("covered", "partial", "gap"):
        raise LLMError(f"verifier 'status' must be covered/partial/gap, got {status!r}")
    severity = d.get("severity", "low")
    if severity not in ("low", "medium", "high"):
        raise LLMError(f"verifier 'severity' must be low/medium/high, got {severity!r}")
    confidence = d.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        raise LLMError(f"verifier 'confidence' out of range: {confidence!r}")
    return {
        "status": status,
        "severity": severity,
        "confidence": float(confidence),
        "evidence": str(d.get("evidence_excerpt", "") or ""),
        "reasoning": str(d.get("reasoning", "") or ""),
        "suggested_remediation": str(d.get("suggested_remediation", "") or ""),
    }


def _verify_with_llm(
    llm: LLMClient,
    policy_text: str,
    article: _Article,
) -> dict:
    """Single article-vs-policy LLM verification. Returns the parsed result."""
    prompt = build_gap_analysis_prompt(
        policy_text=policy_text,
        framework=article.framework,
        reference=article.reference,
        requirement=article.requirement,
        body=article.body,
    )
    enforce_token_budget(prompt)
    resp = llm.complete(prompt)
    # The Response.text from a JSON-format LLM call is the JSON string itself.
    return _parse_verifier_response(resp.text)


# ---------------------------------------------------------------------------
# The analyzer
# ---------------------------------------------------------------------------


def analyze(
    policy_text: str,
    *,
    embedder: Embedder,
    llm_client: LLMClient,
    framework: str | None = None,
    coverage_high: float = COVERAGE_HIGH,
    coverage_low: float = COVERAGE_LOW,
    verify_limit: int = VERIFY_LIMIT,
    verify_top_gaps: int = VERIFY_TOP_GAPS,
) -> GapReport:
    """
    Analyze `policy_text` against the loaded framework articles.

    Raises GapAnalysisRefusal on input rejection or LLM transport failure.

    The coverage scoring re-embeds article bodies on the fly via embedder.embed(),
    so the analyzer does not depend on the Chroma collection layout. SQLite is
    the source of truth for which frameworks/articles are loaded.
    """
    # 1) Input guardrails
    try:
        policy_text = sanitize_text(policy_text, max_len=MAX_POLICY_CHARS)
    except GuardrailViolation as e:
        raise GapAnalysisRefusal(f"policy text rejected: {e}") from e
    if not policy_text.strip():
        raise GapAnalysisRefusal("empty policy text")

    # 2) Chunk + embed the policy
    chunks = chunk_policy(policy_text)
    if not chunks:
        raise GapAnalysisRefusal("no analysable content in policy")
    chunk_vecs = embedder.embed(chunks)

    # 3) Load articles (gateway-audited)
    articles = _load_articles(framework)
    if not articles:
        scope = framework if framework is not None else "all"
        raise GapAnalysisRefusal(f"no articles loaded for framework {scope!r}")

    # 4) Embed article bodies once each
    article_vecs = embedder.embed([a.body for a in articles])

    # 5) Coverage scores + bucketing
    scores: list[float] = [
        score_coverage(av, chunk_vecs) for av in article_vecs
    ]
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

    # 6) LLM verification: every ambiguous, plus the top-N probably_gap by
    #    similarity (worst gaps first), capped by the per-analysis budget.
    probably_gap_idx.sort(key=lambda i: scores[i])
    verify_queue: list[int] = list(ambiguous_idx) + probably_gap_idx[:verify_top_gaps]
    if len(verify_queue) > verify_limit:
        verify_queue = verify_queue[:verify_limit]

    verified: dict[int, dict] = {}
    for i in verify_queue:
        try:
            verified[i] = _verify_with_llm(llm_client, policy_text, articles[i])
        except LLMError as e:
            # One bad verifier call must not poison the whole report. Mark
            # this article as "needs review" and continue.
            verified[i] = {
                "status": "partial",
                "severity": "medium",
                "confidence": 0.0,
                "evidence": "",
                "reasoning": f"LLM verification failed ({e}); flagged for human review.",
                "suggested_remediation": "Manually review this requirement against the policy.",
            }

    # 7) Build findings + redact PII from any evidence excerpts (the policy is
    #    user-supplied and may contain PII the toolkit should not echo back).
    findings: list[Finding] = []
    for i, article in enumerate(articles):
        sc = scores[i]
        if i in verified:
            v = verified[i]
            evidence = redact_pii(v["evidence"]).text if v["evidence"] else ""
            reasoning = redact_pii(v["reasoning"]).text if v["reasoning"] else ""
            remediation = (
                redact_pii(v["suggested_remediation"]).text
                if v["suggested_remediation"] else ""
            )
            findings.append(Finding(
                framework=article.framework,
                reference=article.reference,
                requirement=article.requirement,
                status=v["status"],
                severity=v["severity"],
                confidence=v["confidence"],
                evidence=evidence,
                reasoning=reasoning,
                suggested_remediation=remediation,
            ))
        elif i in set(probably_covered_idx):
            findings.append(Finding(
                framework=article.framework,
                reference=article.reference,
                requirement=article.requirement,
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
            findings.append(Finding(
                framework=article.framework,
                reference=article.reference,
                requirement=article.requirement,
                status="gap",
                severity="medium",
                confidence=min(1.0, max(0.0, 1.0 - sc)),  # low sim -> high gap confidence
                evidence="",
                reasoning=(
                    f"No semantically similar content found in the policy "
                    f"(max chunk similarity {sc:.2f}). Not LLM-verified."
                ),
                suggested_remediation=(
                    f"Consider adding policy language addressing "
                    f"{article.requirement!r} ({article.reference})."
                ),
            ))

    n_covered = sum(1 for f in findings if f.status == "covered")
    n_partial = sum(1 for f in findings if f.status == "partial")
    n_gap = sum(1 for f in findings if f.status == "gap")
    return GapReport(
        framework=framework if framework is not None else "*",
        n_articles=len(articles),
        n_covered=n_covered,
        n_partial=n_partial,
        n_gap=n_gap,
        n_llm_verifications=len(verified),
        findings=findings,
    )
