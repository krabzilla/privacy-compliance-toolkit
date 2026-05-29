"""MCP API-key auth tests.

Members are referenced via the module (``auth.X``) rather than imported by name.
Reason: another test in the session may ``importlib.reload`` src.mcp_server.auth
(test_middleware does), which rebinds class identities like ``AuthError`` in
place. A by-name import would capture the stale pre-reload class and make
``pytest.raises(AuthError)`` miss the freshly-raised one — an order-dependent
failure. Going through the module keeps these tests order-independent.
"""
from __future__ import annotations

import pytest

from src.mcp_server import auth


class TestGenerate:
    def test_length_and_urlsafe(self) -> None:
        k = auth.generate_api_key()
        assert len(k) >= 40
        # token_urlsafe alphabet
        assert all(c.isalnum() or c in "-_" for c in k)

    def test_unique(self) -> None:
        assert auth.generate_api_key() != auth.generate_api_key()


class TestVerify:
    def test_correct_key(self) -> None:
        assert auth.verify_api_key("abc123", expected="abc123") is True

    def test_wrong_key(self) -> None:
        assert auth.verify_api_key("nope", expected="abc123") is False

    def test_empty_provided(self) -> None:
        assert auth.verify_api_key(None, expected="abc123") is False
        assert auth.verify_api_key("", expected="abc123") is False

    def test_empty_expected_denies(self) -> None:
        # No key configured -> nothing is valid.
        assert auth.verify_api_key("anything", expected="") is False


class TestExtractBearer:
    def test_bearer_prefix(self) -> None:
        assert auth.extract_bearer("Bearer xyz") == "xyz"
        assert auth.extract_bearer("bearer xyz") == "xyz"

    def test_raw_value(self) -> None:
        assert auth.extract_bearer("xyz") == "xyz"

    def test_none(self) -> None:
        assert auth.extract_bearer(None) is None
        assert auth.extract_bearer("") is None


class TestConfiguredGuard:
    def test_require_configured_raises_when_empty(self) -> None:
        with pytest.raises(auth.AuthError):
            auth.require_configured(expected="")

    def test_require_configured_ok_when_set(self) -> None:
        auth.require_configured(expected="something")  # no raise

    def test_is_auth_configured(self) -> None:
        assert auth.is_auth_configured(expected="k") is True
        assert auth.is_auth_configured(expected="") is False
