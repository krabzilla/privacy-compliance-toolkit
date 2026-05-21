"""
HTTP-level tests for the security middleware.

Mounts SecurityMiddleware on a plain Starlette app (no FastMCP needed) and
drives it with the test client to prove real 401 / 200 / 429 behaviour.
"""
from __future__ import annotations

from pathlib import Path

import pytest

starlette = pytest.importorskip("starlette")
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

API_KEY = "secret-test-key"


def _build_client(isolated_env: Path, max_requests: int = 100):
    # Reload auth/middleware so they see the key we set via env.
    import importlib

    import src.config
    importlib.reload(src.config)
    import src.logging_gateway
    importlib.reload(src.logging_gateway)
    import src.mcp_server.auth as auth
    importlib.reload(auth)
    import src.mcp_server.rate_limit as rl
    importlib.reload(rl)
    import src.mcp_server.middleware as mw
    importlib.reload(mw)

    async def ok(_request):
        return PlainTextResponse("ok")

    async def health(_request):
        return PlainTextResponse("healthy")

    app = Starlette(routes=[Route("/tool", ok), Route("/health", health)])
    limiter = rl.RateLimiter(max_requests=max_requests, window_seconds=60)
    app.add_middleware(mw.SecurityMiddleware, limiter=limiter)
    return TestClient(app)


@pytest.fixture()
def client(isolated_env, monkeypatch):
    monkeypatch.setenv("PCT_MCP_API_KEY", API_KEY)
    return _build_client(isolated_env)


def test_no_key_is_401(client) -> None:
    r = client.get("/tool")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthorized"


def test_wrong_key_is_401(client) -> None:
    r = client.get("/tool", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_correct_key_passes(client) -> None:
    r = client.get("/tool", headers={"Authorization": f"Bearer {API_KEY}"})
    assert r.status_code == 200
    assert r.text == "ok"


def test_health_is_public(client) -> None:
    r = client.get("/health")  # no auth header
    assert r.status_code == 200


def test_rate_limit_returns_429(isolated_env, monkeypatch) -> None:
    monkeypatch.setenv("PCT_MCP_API_KEY", API_KEY)
    c = _build_client(isolated_env, max_requests=3)
    h = {"Authorization": f"Bearer {API_KEY}"}
    assert c.get("/tool", headers=h).status_code == 200
    assert c.get("/tool", headers=h).status_code == 200
    assert c.get("/tool", headers=h).status_code == 200
    # 4th request within the window is throttled.
    assert c.get("/tool", headers=h).status_code == 429
