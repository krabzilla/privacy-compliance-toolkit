"""
Processing guardrails — Layer 2.

Goals (v0):
  - Bound LLM input size (token budget).
  - Time-box external calls.
  - Pattern-detect obvious prompt-injection attempts (v1 upgrades to a classifier).
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, TypeVar

from ..config import CONFIG
from .input import GuardrailViolation

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Token budget — coarse character heuristic (≈ 4 chars / token for English).
# v1 will swap in `tiktoken` for the configured model.
# ---------------------------------------------------------------------------

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def enforce_token_budget(text: str, *, max_tokens: int | None = None) -> str:
    """Return text unchanged if within budget; raise otherwise."""
    cap = max_tokens or CONFIG.llm_max_tokens
    est = estimate_tokens(text)
    if est > cap:
        raise GuardrailViolation(
            f"prompt ≈{est} tokens exceeds budget of {cap}"
        )
    return text


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


async def run_with_timeout(coro: Awaitable[T], *, seconds: int | None = None) -> T:
    """Wrap an awaitable with the configured request timeout."""
    timeout = seconds or CONFIG.request_timeout_s
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError as e:
        raise GuardrailViolation(f"operation timed out after {timeout}s") from e


# ---------------------------------------------------------------------------
# Prompt-injection pattern detection (v0 — best-effort, not a security
# boundary by itself; the citation-verification step in output guardrails is
# the real backstop).
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    r"\bignore (?:all |the |any )?(?:previous|prior|above) instructions?\b",
    r"\bdisregard (?:all |the |any )?(?:previous|prior|above) instructions?\b",
    r"\bforget (?:everything|all|prior)\b",
    r"\byou are now\b.*\b(?:dan|jailbroken|unrestricted)\b",
    r"\bsystem prompt\b.*\b(?:reveal|show|print|dump|leak)\b",
    r"<\|\s*im_start\s*\|>",                # OpenAI chat-format smuggling
    r"<\s*/?\s*(?:system|assistant|user)\s*>",  # tag-style role smuggling
]

_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def detect_injection(text: str) -> list[str]:
    """Return a list of matched injection patterns. Empty list = clean."""
    if not isinstance(text, str):
        return []
    return [m.group(0) for m in _INJECTION_RE.finditer(text)]
