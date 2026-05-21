"""
In-memory per-key rate limiter (sliding window).

Good enough for a single-process MCP server. For a multi-process / multi-host
deployment (v2), swap the backing store for Redis with the same interface.

The clock is injectable (`now` parameter) so tests are deterministic and do
not need to sleep.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max_requests = max_requests
        self.window_seconds = float(window_seconds)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, now: float | None = None) -> bool:
        """
        Record a request for `key` and return True if it is allowed, False if
        the key has exceeded its budget within the window.
        """
        ts = time.monotonic() if now is None else now
        cutoff = ts - self.window_seconds
        with self._lock:
            dq = self._hits[key]
            # Drop timestamps older than the window.
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self.max_requests:
                return False
            dq.append(ts)
            return True

    def remaining(self, key: str, *, now: float | None = None) -> int:
        """How many more requests `key` may make right now."""
        ts = time.monotonic() if now is None else now
        cutoff = ts - self.window_seconds
        with self._lock:
            dq = self._hits[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            return max(0, self.max_requests - len(dq))

    def reset(self, key: str | None = None) -> None:
        """Clear state for one key, or all keys if key is None."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
