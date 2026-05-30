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
