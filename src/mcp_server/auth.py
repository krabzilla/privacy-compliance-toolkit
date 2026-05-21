"""
MCP API-key authentication.

Design notes:
  - The key is NEVER stored in code. It lives only in the environment
    (PCT_MCP_API_KEY), generated once via scripts/generate_api_key.py.
  - Comparisons use secrets.compare_digest to avoid timing side-channels.
  - The server refuses to boot over HTTP with no key configured
    (fail securely / secure default — no accidental open server).

This module deliberately imports nothing from fastmcp so it can be unit
tested in isolation.
"""
from __future__ import annotations

import secrets

from ..config import CONFIG


class AuthError(RuntimeError):
    """Raised when authentication cannot be guaranteed (e.g. no key configured)."""


def generate_api_key(nbytes: int = 32) -> str:
    """
    Generate a cryptographically secure, URL-safe API key.

    Run once on your machine (not in app code). 32 bytes -> ~43 chars of
    base64url, ~256 bits of entropy.
    """
    return secrets.token_urlsafe(nbytes)


def is_auth_configured(expected: str | None = None) -> bool:
    """True if an API key is configured."""
    key = CONFIG.mcp_api_key if expected is None else expected
    return bool(key)


def require_configured(expected: str | None = None) -> None:
    """
    Startup guard. Raise if no key is configured.

    Called before the HTTP server boots so an unauthenticated server can
    never start by accident.
    """
    if not is_auth_configured(expected):
        raise AuthError(
            "PCT_MCP_API_KEY is not set -- refusing to start an unauthenticated "
            "HTTP server. Generate a key with `python scripts/generate_api_key.py` "
            "and export it before running the server."
        )


def extract_bearer(header_value: str | None) -> str | None:
    """
    Pull the credential out of an Authorization header.

    Accepts both 'Authorization: Bearer <key>' and a raw '<key>' value.
    Returns None if nothing usable is present.
    """
    if not header_value or not isinstance(header_value, str):
        return None
    value = header_value.strip()
    parts = value.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return value or None


def verify_api_key(provided: str | None, expected: str | None = None) -> bool:
    """
    Constant-time comparison of a provided key against the configured key.

    Returns False (never raises) for any missing/empty input so callers can
    treat a falsy result as 'deny'.
    """
    exp = CONFIG.mcp_api_key if expected is None else expected
    if not exp or not provided:
        return False
    return secrets.compare_digest(str(provided), str(exp))
