"""
HTTP security middleware (auth + rate limiting) -- the network-edge guardrail.

Separation of duties: this module knows nothing about FastMCP or the tools.
It only enforces the security contract on incoming HTTP requests:

  1. Extract the bearer token from the Authorization header.
  2. Constant-time compare against PCT_MCP_API_KEY  -> 401 on mismatch.
  3. Per-key sliding-window rate limit               -> 429 when exceeded.

Every denial is recorded through the logging gateway (audited, not silent).
The raw API key is never logged -- only a short, non-reversible fingerprint.

Keeping this fastmcp-free means it can be tested over real HTTP with a plain
Starlette app (see tests/test_middleware.py).
"""
from __future__ import annotations

import hashlib

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from ..config import CONFIG
from ..logging_gateway import gateway
from .auth import extract_bearer, verify_api_key
from .rate_limit import RateLimiter

# Liveness endpoints are exempt so health probes don't need the key.
PUBLIC_PATHS = {"/health", "/healthz", "/ping"}


def key_fingerprint(token: str) -> str:
    """Short, non-reversible tag for audit logs -- never log the raw key."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def make_limiter() -> RateLimiter:
    return RateLimiter(
        max_requests=CONFIG.mcp_rate_limit_requests,
        window_seconds=CONFIG.mcp_rate_limit_window_s,
    )


def make_edge_limiter() -> RateLimiter:
    """
    Per-IP flood guard that runs BEFORE auth. Deliberately more permissive than
    the per-key limit (one host may legitimately carry several keys); its only
    job is to cap the unauthenticated path so a flood of bad-key requests cannot
    force one fsync'd audit row per request (a disk-I/O amplification DoS).
    """
    return RateLimiter(
        max_requests=max(CONFIG.mcp_rate_limit_requests * 5, CONFIG.mcp_rate_limit_requests),
        window_seconds=CONFIG.mcp_rate_limit_window_s,
    )


class SecurityMiddleware(BaseHTTPMiddleware):
    """Starlette middleware enforcing API-key auth + per-key rate limiting."""

    def __init__(
        self,
        app,
        limiter: RateLimiter | None = None,
        edge_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__(app)
        self.limiter = limiter or make_limiter()
        self.edge_limiter = edge_limiter or make_edge_limiter()

    async def dispatch(self, request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        # Edge guard: cheap, in-memory, per-IP limit BEFORE auth or any audit
        # write. This bounds the unauthenticated deny() path below, which fsyncs
        # an audit line per call -- without this, an unauthenticated flood is an
        # un-rate-limited disk-I/O amplification vector. No audit write here on
        # purpose: writing would reintroduce the amplification we are stopping.
        # NOTE: request.client.host is the immediate peer; behind a reverse proxy
        # (v2) trust a vetted X-Forwarded-For instead.
        client_ip = request.client.host if request.client else "unknown"
        if not self.edge_limiter.check(client_ip):
            return JSONResponse({"error": "rate_limited"}, status_code=429)

        token = extract_bearer(request.headers.get("authorization"))

        if not verify_api_key(token):
            await run_in_threadpool(
                gateway.deny,
                actor="mcp.http",
                action="auth",
                resource=request.url.path,
                reason="missing or invalid API key",
            )
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        fp = key_fingerprint(token)
        if not self.limiter.check(fp):
            await run_in_threadpool(
                gateway.deny,
                actor="mcp.http",
                action="rate_limit",
                resource=request.url.path,
                reason=f"rate limit exceeded for key {fp}",
            )
            return JSONResponse({"error": "rate_limited"}, status_code=429)

        return await call_next(request)
