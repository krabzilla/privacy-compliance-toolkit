"""
RAG engine -- the librarian becomes an analyst.

Pipeline (every step has a clear failure mode):

  1) sanitize the question           input guardrail; RAGRefusal on violation
  2) retrieve top-k via VectorStore  every retrieval audited via gateway
  3) compose the structured prompt   build_prompt() with the data-not-instructions
                                     preamble and delimited <RULES>/<QUESTION>
  4) enforce token budget            processing guardrail; RAGRefusal if exceeded
  5) call the LLM                    LLMClient.complete(); any LLMError -> RAGRefusal
  6) enforce confidence floor        output guardrail; RAGRefusal if below threshold
  7) validate STRUCTURED citations   every citation in the response must be in BOTH
                                     the retrieved set AND the known-references set;
                                     any miss -> RAGRefusal. This is v1.2's central
                                     thesis: the engine never has to extract citations
                                     from prose because the LLM emits them in a
                                     separate field. The non-canonical-hallucination
                                     gap that v0.1 documented as xfail does not apply
                                     here -- structured emission sidesteps it.
  8) redact PII from the answer      output guardrail
  9) return Answer with the verified citation set

Dependency injection: vector_store and llm_client are REQUIRED parameters.
The engine itself is pure -- no global singletons, no implicit defaults.
The caller (the MCP tool, the script, the test) decides what to wire in.
That makes the engine trivially testable with FakeLLMClient + FakeEmbedder
+ in-memory Chroma, and means every call site is honest about what it is
asking the analyst to use.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import CONFIG
from ..guardrails.input import GuardrailViolation, sanitize_text
from ..guardrails.output import enforce_confidence, redact_pii
from ..guardrails.processing import enforce_token_budget
from ..logging_gateway import gateway
from ..llm.client import Citation, LLMClient, LLMError
from .vector_store import SearchHit, VectorStore
from ..llm.prompts import build_prompt


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


class RAGRefusal(GuardrailViolation):
    """The pipeline refused to return an answer. Reason is in the message."""


@dataclass(frozen=True)
class Answer:
    text: str
    citations: list[Citation]
    confidence: float
    retrieved_refs: list[str]  # what we showed the model, for transparency


# ---------------------------------------------------------------------------
# Helper -- known references (every reference in the DB across all frameworks)
# ---------------------------------------------------------------------------


def _all_known_references() -> set[str]:
    """The full reference set the citation verifier checks against."""
    with gateway.access(
        actor="rag.engine.internal",
        action="read",
        resource="articles:all_references",
    ) as ctx:
        rows = ctx.fetch_all("SELECT reference FROM articles")
    return {r["reference"] for r in rows}


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def answer(
    question: str,
    *,
    vector_store: VectorStore,
    llm_client: LLMClient,
    framework: str | None = None,
    k: int = 5,
    confidence_threshold: float | None = None,
) -> Answer:
    """
    Answer `question` using retrieved framework rules, with verified citations.

    Raises RAGRefusal on any guardrail violation (no rules retrieved, low
    confidence, fabricated citation, LLM transport error, PII pipeline
    failure, etc.). The caller surfaces RAGRefusal to the user as an explicit
    refusal -- never as a partial answer.
    """
    # 1) Input guardrail
    try:
        q = sanitize_text(question, max_len=2000)
    except GuardrailViolation as e:
        raise RAGRefusal(f"input rejected: {e}") from e

    if not q:
        raise RAGRefusal("empty question")

    # 2) Retrieve (gateway-audited inside VectorStore.query)
    hits: list[SearchHit] = vector_store.query(q, k=k, framework=framework)
    if not hits:
        raise RAGRefusal(
            "no relevant rules retrieved -- cannot answer without grounding"
        )

    # 3) Compose
    prompt = build_prompt(q, hits)

    # 4) Processing guardrail
    try:
        enforce_token_budget(prompt)
    except GuardrailViolation as e:
        raise RAGRefusal(f"prompt over token budget: {e}") from e

    # 5) Generate
    try:
        resp = llm_client.complete(prompt)
    except LLMError as e:
        raise RAGRefusal(f"LLM call failed: {e}") from e

    # 6) Confidence floor
    try:
        enforce_confidence(resp.confidence, threshold=confidence_threshold)
    except GuardrailViolation as e:
        raise RAGRefusal(str(e)) from e

    # 7) Citation validation -- the v1.2 thesis. Every citation must be:
    #    (a) one we actually retrieved (model can't cite rules it didn't see)
    #    (b) a real article in the loaded corpus (catches typos / cross-framework
    #        drift; redundant with (a) for well-behaved models but cheap insurance)
    retrieved_refs = {h.reference for h in hits}
    known_refs = _all_known_references()
    for c in resp.citations:
        if c.reference not in retrieved_refs:
            raise RAGRefusal(
                f"citation {c.reference!r} not in retrieved rules "
                f"(model cited what it was not shown -- refusing)"
            )
        if c.reference not in known_refs:
            raise RAGRefusal(
                f"citation {c.reference!r} is not a known article "
                f"(refusing rather than passing through unverified)"
            )

    # 8) Output guardrail -- PII redaction on the human-readable text
    text = redact_pii(resp.text).text

    return Answer(
        text=text,
        citations=list(resp.citations),
        confidence=resp.confidence,
        retrieved_refs=sorted(retrieved_refs),
    )
