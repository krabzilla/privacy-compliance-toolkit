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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    print(
        f"Privacy Compliance Toolkit MCP — listening on {CONFIG.mcp_host}:{CONFIG.mcp_port}",
    )
    # FastMCP's transport options vary by version; stdio is the safest default
    # for local development. Swap to mcp.run(transport='sse', host=..., port=...)
    # once the front-end is wired up in v2.
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()


# Suppress unused-import warning at module level
_ = json
