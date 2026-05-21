"""Rate limiter tests — deterministic via injected clock."""
from __future__ import annotations

from src.mcp_server.rate_limit import RateLimiter


def test_allows_up_to_limit() -> None:
    rl = RateLimiter(max_requests=3, window_seconds=10)
    assert rl.check("k", now=0) is True
    assert rl.check("k", now=1) is True
    assert rl.check("k", now=2) is True
    # 4th within window -> blocked
    assert rl.check("k", now=3) is False


def test_window_expiry_frees_budget() -> None:
    rl = RateLimiter(max_requests=2, window_seconds=10)
    assert rl.check("k", now=0) is True
    assert rl.check("k", now=1) is True
    assert rl.check("k", now=2) is False
    # After the window passes, old hits drop off.
    assert rl.check("k", now=12) is True


def test_keys_are_isolated() -> None:
    rl = RateLimiter(max_requests=1, window_seconds=10)
    assert rl.check("a", now=0) is True
    assert rl.check("a", now=1) is False
    # Different key has its own budget.
    assert rl.check("b", now=1) is True


def test_remaining() -> None:
    rl = RateLimiter(max_requests=3, window_seconds=10)
    assert rl.remaining("k", now=0) == 3
    rl.check("k", now=0)
    assert rl.remaining("k", now=0) == 2


def test_reset() -> None:
    rl = RateLimiter(max_requests=1, window_seconds=10)
    rl.check("k", now=0)
    assert rl.check("k", now=1) is False
    rl.reset("k")
    assert rl.check("k", now=1) is True
