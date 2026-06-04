"""
FastMCP server — v0 tools.

Every tool:
  1. Validates and sanitises arguments at the door (input guardrails).
  2. Goes through the logging gateway for any DB read.
  3. Redacts PII and verifies citations on the way out (output guardrails).
  4. Returns a clean refusal on guardrail violation (loud, not silent).

Run:
    python -m src.mcp_server.server
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from ..config import CONFIG
from ..guardrails.input import GuardrailViolation, sanitize_text
from ..guardrails.output import redact_pii, verify_citations
from ..logging_gateway import gateway

try:
    from fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "fastmcp not installed. Run: pip install -r requirements.txt"
    ) from e


mcp = FastMCP("privacy-compliance-toolkit")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_request_id() -> str:
    return uuid.uuid4().hex


def _refusal(reason: str, *, request_id: str) -> dict[str, Any]:
    """Standard refusal envelope. Never include caller-controlled data verbatim."""
    return {
        "ok": False,
        "request_id": request_id,
        "error": "guardrail_violation",
        "reason": reason[:300],
    }


def _ok(payload: dict[str, Any], *, request_id: str) -> dict[str, Any]:
    return {"ok": True, "request_id": request_id, **payload}


def _all_known_references() -> list[str]:
    """Pull every Reference row from the DB. Used by the citation verifier."""
    with gateway.access(
        actor="mcp.internal",
        action="read",
        resource="articles:all_references",
    ) as ctx:
        rows = ctx.fetch_all("SELECT reference FROM articles")
    return [r["reference"] for r in rows]


# Module-level VectorStore singleton -- lazy so SQLite-only tools never pay
# the sentence-transformers model load. Tests inject a FakeEmbedder-backed
# store via set_vector_store(); production code calls get_vector_store().
_vector_store = None  # type: ignore[var-annotated]


def get_vector_store():
    """Return the active VectorStore, constructing the default on first call."""
    global _vector_store
    if _vector_store is None:
        from ..rag import SentenceTransformerEmbedder, VectorStore
        _vector_store = VectorStore(
            embedder=SentenceTransformerEmbedder(),
            persist_dir=CONFIG.chroma_dir,
        )
    return _vector_store


def set_vector_store(vs) -> None:
    """Inject a VectorStore (or None to force re-init). Used by tests."""
    global _vector_store
    _vector_store = vs


# Module-level LLM client singleton -- lazy for the same reason as the
# vector store. Tests inject a FakeLLMClient; production wires the
# configured provider (Ollama by default).
_llm_client = None  # type: ignore[var-annotated]


def get_llm_client():
    """Return the active LLMClient, constructing the configured default if absent."""
    global _llm_client
    if _llm_client is None:
        from ..llm import OllamaClient
        # v1.2 ships Ollama only; the wrapper makes adding GeminiClient /
        # GroqClient a one-class change rather than a server-wide refactor.
        _llm_client = OllamaClient(
            model=CONFIG.llm_model,
            base_url=CONFIG.ollama_base_url,
            timeout_s=CONFIG.request_timeout_s,
        )
    return _llm_client


def set_llm_client(client) -> None:
    """Inject an LLMClient (or None to force re-init). Used by tests."""
    global _llm_client
    _llm_client = client


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_frameworks() -> dict[str, Any]:
    """List the compliance frameworks currently loaded in the toolkit."""
    req_id = _new_request_id()
    with gateway.access(
        actor="mcp.list_frameworks",
        action="read",
        resource="frameworks:*",
        request_id=req_id,
    ) as ctx:
        rows = ctx.fetch_all(
            """
            SELECT f.name, f.version, f.loaded_at, COUNT(a.id) AS article_count
            FROM frameworks f
            LEFT JOIN articles a ON a.framework_id = f.id
            GROUP BY f.id
            ORDER BY f.name
            """
        )
    return _ok(
        {
            "frameworks": [
                {
                    "name": r["name"],
                    "version": r["version"],
                    "loaded_at": r["loaded_at"],
                    "article_count": r["article_count"],
                }
                for r in rows
            ]
        },
        request_id=req_id,
    )


@mcp.tool()
def get_article(framework: str, reference: str) -> dict[str, Any]:
    """
    Fetch a single article by its canonical reference (e.g. 'GDPR Art. 6').

    Args:
        framework: framework name as registered (e.g. 'GDPR').
        reference: canonical citation string.
    """
    req_id = _new_request_id()
    try:
        framework = sanitize_text(framework, max_len=100)
        reference = sanitize_text(reference, max_len=200)
    except GuardrailViolation as e:
        gateway.deny(
            actor="mcp.get_article",
            action="read",
            resource=f"articles:{reference!s}",
            reason=str(e),
            request_id=req_id,
        )
        return _refusal(str(e), request_id=req_id)

    with gateway.access(
        actor="mcp.get_article",
        action="read",
        resource=f"articles:{reference}",
        request_id=req_id,
        metadata={"framework": framework},
    ) as ctx:
        row = ctx.fetch_one(
            """
            SELECT a.category, a.requirement, a.body, a.reference,
                   f.name AS framework, f.version
            FROM articles a
            JOIN frameworks f ON f.id = a.framework_id
            WHERE f.name = ? AND a.reference = ?
            """,
            (framework, reference),
        )

    if not row:
        return _refusal(f"not found: {reference}", request_id=req_id)

    body = redact_pii(row["body"]).text
    # Self-check: any citation in the body must trace back to a known reference.
    bad = verify_citations(body, _all_known_references())
    if bad:
        return _refusal(
            f"citation verification failed for: {', '.join(bad[:3])}",
            request_id=req_id,
        )

    return _ok(
        {
            "framework": row["framework"],
            "version": row["version"],
            "category": row["category"],
            "requirement": row["requirement"],
            "reference": row["reference"],
            "body": body,
        },
        request_id=req_id,
    )


@mcp.tool()
def search_frameworks(query: str, limit: int = 10) -> dict[str, Any]:
    """
    Keyword search across loaded frameworks (LIKE-based; ChromaDB RAG comes in v1).

    Args:
        query: free-text search string.
        limit: max results (1-50, default 10).
    """
    req_id = _new_request_id()
    try:
        query = sanitize_text(query, max_len=500)
    except GuardrailViolation as e:
        gateway.deny(
            actor="mcp.search_frameworks",
            action="search",
            resource="articles:search",
            reason=str(e),
            request_id=req_id,
        )
        return _refusal(str(e), request_id=req_id)

    limit = max(1, min(int(limit) if limit else 10, 50))
    like = f"%{query}%"

    with gateway.access(
        actor="mcp.search_frameworks",
        action="search",
        resource="articles:search",
        request_id=req_id,
        metadata={"query_len": len(query), "limit": limit},
    ) as ctx:
        rows = ctx.fetch_all(
            """
            SELECT f.name AS framework, a.category, a.requirement,
                   a.reference, substr(a.body, 1, 240) AS snippet
            FROM articles a
            JOIN frameworks f ON f.id = a.framework_id
            WHERE a.body LIKE ? OR a.requirement LIKE ? OR a.category LIKE ?
            LIMIT ?
            """,
            (like, like, like, limit),
        )

    results = []
    for r in rows:
        snippet = redact_pii(r["snippet"]).text
        results.append(
            {
                "framework": r["framework"],
                "category": r["category"],
                "requirement": r["requirement"],
                "reference": r["reference"],
                "snippet": snippet,
            }
        )
    return _ok({"query": query, "count": len(results), "results": results}, request_id=req_id)


@mcp.tool()
def semantic_search(query: str, k: int = 5, framework: str | None = None) -> dict[str, Any]:
    """
    Semantic search over loaded frameworks (RAG retrieval; ChromaDB-backed).

    Returns the top-k articles most similar in meaning to `query`. Unlike
    search_frameworks, this does NOT require keyword overlap -- "when am I
    allowed to use someones data" can rank GDPR Art. 6 highly even though the
    query never says "consent" or "Art. 6".

    Args:
        query: free-text search string.
        k: max results (1-20, default 5).
        framework: optional framework name to scope (e.g. 'GDPR'); None searches all.
    """
    req_id = _new_request_id()
    try:
        query = sanitize_text(query, max_len=500)
        if framework is not None:
            framework = sanitize_text(framework, max_len=100)
    except GuardrailViolation as e:
        gateway.deny(
            actor="mcp.semantic_search",
            action="search",
            resource="chroma:semantic_search",
            reason=str(e),
            request_id=req_id,
        )
        return _refusal(str(e), request_id=req_id)

    k = max(1, min(int(k) if k else 5, 20))

    # vs.query() opens its own gateway-audited read scope, so this MCP tool's
    # call gets a chroma:* audit row without needing to re-wrap it here.
    vs = get_vector_store()
    hits = vs.query(query, k=k, framework=framework)

    results = []
    for h in hits:
        snippet = redact_pii(h.body[:240]).text
        results.append(
            {
                "framework": h.framework,
                "reference": h.reference,
                "category": h.category,
                "requirement": h.requirement,
                "snippet": snippet,
                "score": round(h.score, 4),
            }
        )

    return _ok(
        {"query": query, "count": len(results), "results": results},
        request_id=req_id,
    )


@mcp.tool()
def ask_compliance(question: str, framework: str | None = None) -> dict[str, Any]:
    """
    Answer a free-form privacy/compliance question with verified citations.

    The RAG engine retrieves the most relevant rules, asks the configured
    LLM to answer using ONLY those rules, and then validates every emitted
    citation against (a) what was actually retrieved and (b) the known
    reference set. A fabricated, mis-remembered, or low-confidence citation
    causes the entire answer to be REFUSED -- never partially returned.

    Args:
        question: free-text privacy / compliance question.
        framework: optional framework name to scope retrieval
                   (e.g. 'GDPR', 'ISO 27701').
    """
    req_id = _new_request_id()
    try:
        question = sanitize_text(question, max_len=2000)
        if framework is not None:
            framework = sanitize_text(framework, max_len=100)
    except GuardrailViolation as e:
        gateway.deny(
            actor="mcp.ask_compliance",
            action="ask",
            resource="rag:engine",
            reason=str(e),
            request_id=req_id,
        )
        return _refusal(str(e), request_id=req_id)

    from ..rag.engine import RAGRefusal, answer as rag_answer

    try:
        a = rag_answer(
            question,
            vector_store=get_vector_store(),
            llm_client=get_llm_client(),
            framework=framework,
        )
    except RAGRefusal as e:
        # RAGRefusal already audited (the gateway scopes inside the engine
        # captured the retrieval / write events). Surface as a clean refusal.
        return _refusal(str(e), request_id=req_id)

    return _ok(
        {
            "question": question,
            "answer": a.text,
            "citations": [
                {"framework": c.framework, "reference": c.reference}
                for c in a.citations
            ],
            "confidence": round(a.confidence, 4),
            "retrieved_refs": a.retrieved_refs,
        },
        request_id=req_id,
    )


@mcp.tool()
def analyze_policy(policy_text: str, framework: str) -> dict[str, Any]:
    """
    Compare a privacy policy against a single loaded framework, return per-
    requirement findings (covered / partial / gap) with severity, evidence,
    reasoning, and suggested remediation.

    Hybrid pipeline (v1.3):
      * Semantic coverage scoring decides articles with clearly high or low
        similarity in milliseconds.
      * Ambiguous cases and the top-N likely gaps are verified with the
        configured LLM (Ollama by default). Verification is bounded by a
        per-analysis budget so total time stays under a couple of minutes.

    Args:
        policy_text: free-text privacy policy (UTF-8, up to ~50,000 chars).
        framework: one of the loaded framework names (e.g. "GDPR",
                   "ISO 27701", "Danish DPA", "NIST CSF").
    """
    req_id = _new_request_id()
    try:
        policy_text = sanitize_text(policy_text, max_len=50_000)
        framework = sanitize_text(framework, max_len=100)
    except GuardrailViolation as e:
        gateway.deny(
            actor="mcp.analyze_policy",
            action="analyze",
            resource=f"gap_analysis:{framework!s}",
            reason=str(e),
            request_id=req_id,
        )
        return _refusal(str(e), request_id=req_id)

    from ..rag.embeddings import SentenceTransformerEmbedder
    from ..rag.gap_analysis import GapAnalysisRefusal, analyze as run_analysis

    vs = get_vector_store()
    # The analyzer needs a raw embedder for per-article scoring. Reuse the
    # vector_store's if it exposes one; otherwise fall back to the production
    # default. (vector_store carries an embedder internally; we keep them in
    # sync to avoid two different embedding models in one analysis.)
    embedder = getattr(vs, "_embedder", None) or SentenceTransformerEmbedder()

    try:
        report = run_analysis(
            policy_text,
            embedder=embedder,
            llm_client=get_llm_client(),
            framework=framework,
        )
    except GapAnalysisRefusal as e:
        return _refusal(str(e), request_id=req_id)

    return _ok(report.to_dict(), request_id=req_id)


@mcp.tool()
def analyze_policy_all(policy_text: str) -> dict[str, Any]:
    """
    Same as analyze_policy but runs against every loaded framework at once.
    Per-framework findings are grouped in the response. Slower than a
    single-framework call (more LLM verifications), but a stronger single-
    request demonstration.

    Args:
        policy_text: free-text privacy policy (UTF-8, up to ~50,000 chars).
    """
    req_id = _new_request_id()
    try:
        policy_text = sanitize_text(policy_text, max_len=50_000)
    except GuardrailViolation as e:
        gateway.deny(
            actor="mcp.analyze_policy_all",
            action="analyze",
            resource="gap_analysis:all",
            reason=str(e),
            request_id=req_id,
        )
        return _refusal(str(e), request_id=req_id)

    from ..rag.embeddings import SentenceTransformerEmbedder
    from ..rag.gap_analysis import GapAnalysisRefusal, analyze as run_analysis

    vs = get_vector_store()
    embedder = getattr(vs, "_embedder", None) or SentenceTransformerEmbedder()

    try:
        report = run_analysis(
            policy_text,
            embedder=embedder,
            llm_client=get_llm_client(),
            framework=None,  # all frameworks
        )
    except GapAnalysisRefusal as e:
        return _refusal(str(e), request_id=req_id)

    # Regroup findings by framework for a nicer top-level shape.
    by_fw: dict[str, list[dict]] = {}
    for f in report.findings:
        by_fw.setdefault(f.framework, []).append(f.to_dict())
    per_framework_summary = []
    for fw, items in by_fw.items():
        per_framework_summary.append({
            "framework": fw,
            "n_articles": len(items),
            "n_covered": sum(1 for i in items if i["status"] == "covered"),
            "n_partial": sum(1 for i in items if i["status"] == "partial"),
            "n_gap": sum(1 for i in items if i["status"] == "gap"),
            "findings": items,
        })

    return _ok(
        {
            "n_articles": report.n_articles,
            "n_llm_verifications": report.n_llm_verifications,
            "per_framework": per_framework_summary,
        },
        request_id=req_id,
    )




@mcp.tool()
def analyze_notice(
    policy_text: str,
    org_profile: list[str] | None = None,
) -> dict[str, Any]:
    """
    Check a privacy NOTICE against the curated GDPR notice-requirement
    checklist (Arts. 12-14 + Danish CPR overlay), NOT the full 99-article
    regulation. This is the correct tool for grading a public privacy policy:
    operational duties (ROPA, DPIA, security of processing) are intentionally
    excluded because they do not belong in a notice.

    Each requirement is filtered by the declared org profile, then scored
    semantically and (for borderline cases) verified by the LLM against only
    the most relevant policy passages.

    Args:
        policy_text: free-text privacy notice (UTF-8, up to ~50,000 chars).
        org_profile: facts the org declares, drawn from the checklist
            condition vocabulary, e.g. ["data_collected_directly",
            "legal_basis_includes_consent", "transfers_outside_eea",
            "cpr_processed"]. Requirements whose condition is absent are
            reported as not_applicable rather than as gaps. `always`
            requirements are graded regardless.
    """
    req_id = _new_request_id()
    try:
        policy_text = sanitize_text(policy_text, max_len=50_000)
        profile = {sanitize_text(c, max_len=100) for c in (org_profile or [])}
    except GuardrailViolation as e:
        gateway.deny(
            actor="mcp.analyze_notice",
            action="analyze",
            resource="notice_analysis:gdpr",
            reason=str(e),
            request_id=req_id,
        )
        return _refusal(str(e), request_id=req_id)

    from ..rag.embeddings import SentenceTransformerEmbedder
    from ..rag.notice_analysis import NoticeAnalysisRefusal, analyze_notice as run_notice

    vs = get_vector_store()
    embedder = getattr(vs, "_embedder", None) or SentenceTransformerEmbedder()

    # Keep the audited-access discipline for every analyze call (the checklist
    # itself is a local file, but we still record that an analysis happened).
    with gateway.access(
        actor="mcp.analyze_notice",
        action="analyze",
        resource="notice_analysis:gdpr",
        request_id=req_id,
        metadata={"profile": sorted(profile)},
    ):
        pass

    try:
        report = run_notice(
            policy_text,
            embedder=embedder,
            llm_client=get_llm_client(),
            org_profile=profile,
        )
    except NoticeAnalysisRefusal as e:
        return _refusal(str(e), request_id=req_id)

    return _ok(report.to_dict(), request_id=req_id)

# ---------------------------------------------------------------------------
# Authenticated HTTP app factory
# ---------------------------------------------------------------------------
#
# The security layer (auth + rate limiting) lives in middleware.py, which is
# deliberately free of any FastMCP dependency so it can be tested over real
# HTTP on its own. Here we just attach it to the FastMCP HTTP app.

from .auth import require_configured  # noqa: E402
from .middleware import SecurityMiddleware  # noqa: E402


def build_app():  # pragma: no cover - exercised at runtime, not in unit tests
    """
    Build the authenticated HTTP ASGI app.

    Fails loud if no API key is configured (secure default). The exact FastMCP
    factory name varies by version; http_app() is current. For TLS, terminate
    at uvicorn (ssl_keyfile/ssl_certfile) or a reverse proxy.
    """
    require_configured()
    app = mcp.http_app()
    app.add_middleware(SecurityMiddleware)
    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:  # pragma: no cover
    import uvicorn

    print(
        f"Privacy Compliance Toolkit MCP (HTTP, authenticated) -- "
        f"listening on {CONFIG.mcp_host}:{CONFIG.mcp_port}"
    )
    uvicorn.run(build_app(), host=CONFIG.mcp_host, port=CONFIG.mcp_port)


if __name__ == "__main__":  # pragma: no cover
    main()


# Suppress unused-import warning at module level
_ = json
