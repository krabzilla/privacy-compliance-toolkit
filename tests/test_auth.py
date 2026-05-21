"""MCP API-key auth tests."""
from __future__ import annotations

import pytest

from src.mcp_server.auth import (
    AuthError,
    extract_bearer,
    generate_api_key,
    is_auth_configured,
    require_configured,
    verify_api_key,
)


class TestGenerate:
    def test_length_and_urlsafe(self) -> None:
        k = generate_api_key()
        assert len(k) >= 40
        # token_urlsafe alphabet
        assert all(c.isalnum() or c in "-_" for c in k)

    def test_unique(self) -> None:
        assert generate_api_key() != generate_api_key()


class TestVerify:
    def test_correct_key(self) -> None:
        assert verify_api_key("abc123", expected="abc123") is True

    def test_wrong_key(self) -> None:
        assert verify_api_key("nope", expected="abc123") is False

    def test_empty_provided(self) -> None:
        assert verify_api_key(None, expected="abc123") is False
        assert verify_api_key("", expected="abc123") is False

    def test_empty_expected_denies(self) -> None:
        # No key configured -> nothing is valid.
        assert verify_api_key("anything", expected="") is False


class TestExtractBearer:
    def test_bearer_prefix(self) -> None:
        assert extract_bearer("Bearer xyz") == "xyz"
        assert extract_bearer("bearer xyz") == "xyz"

    def test_raw_value(self) -> None:
        assert extract_bearer("xyz") == "xyz"

    def test_none(self) -> None:
        assert extract_bearer(None) is None
        assert extract_bearer("") is None


class TestConfiguredGuard:
    def test_require_configured_raises_when_empty(self) -> None:
        with pytest.raises(AuthError):
            require_configured(expected="")

    def test_require_configured_ok_when_set(self) -> None:
        require_configured(expected="something")  # no raise

    def test_is_auth_configured(self) -> None:
        assert is_auth_configured(expected="k") is True
        assert is_auth_configured(expected="") is False
