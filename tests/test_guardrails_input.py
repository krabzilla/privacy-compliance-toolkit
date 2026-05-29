"""Input guardrail tests — SSRF, sanitization, size."""
from __future__ import annotations

import pytest

from src.guardrails.input import (
    GuardrailViolation,
    sanitize_text,
    validate_file_size,
    validate_url,
)


class TestValidateURL:
    def test_accepts_https(self, monkeypatch) -> None:
        # Inject a public IP so this stays deterministic and offline; the SSRF
        # check resolves the hostname and re-checks every resolved address.
        import src.guardrails.input as gi

        monkeypatch.setattr(gi, "_resolve_host", lambda host: ["93.184.216.34"])
        assert validate_url("https://example.com/path") == "https://example.com/path"

    def test_rejects_non_http(self) -> None:
        with pytest.raises(GuardrailViolation):
            validate_url("file:///etc/passwd")
        with pytest.raises(GuardrailViolation):
            validate_url("ftp://example.com")

    def test_blocks_loopback_ipv4(self) -> None:
        with pytest.raises(GuardrailViolation):
            validate_url("http://127.0.0.1/")

    def test_blocks_rfc1918(self) -> None:
        for ip in ("10.0.0.1", "192.168.1.1", "172.16.0.1"):
            with pytest.raises(GuardrailViolation):
                validate_url(f"http://{ip}/")

    def test_blocks_link_local(self) -> None:
        with pytest.raises(GuardrailViolation):
            validate_url("http://169.254.169.254/latest/meta-data/")

    def test_blocks_metadata_hostname(self) -> None:
        with pytest.raises(GuardrailViolation):
            validate_url("http://metadata.google.internal/computeMetadata/v1/")

    def test_rejects_oversize_url(self) -> None:
        url = "https://example.com/" + "a" * 3000
        with pytest.raises(GuardrailViolation):
            validate_url(url)


class TestSanitizeText:
    def test_strips_control_chars(self) -> None:
        assert sanitize_text("hello\x01world") == "helloworld"

    def test_rejects_null_byte(self) -> None:
        with pytest.raises(GuardrailViolation):
            sanitize_text("hello\x00world")

    def test_length_cap(self) -> None:
        with pytest.raises(GuardrailViolation):
            sanitize_text("x" * 20_001, max_len=20_000)

    def test_preserves_newlines_and_tabs(self) -> None:
        assert sanitize_text("a\nb\tc") == "a\nb\tc"

    def test_strip_html_optional(self) -> None:
        assert sanitize_text("<b>hi</b>", strip_html=True) == "hi"
        assert sanitize_text("<b>hi</b>") == "<b>hi</b>"


class TestFileSize:
    def test_accepts_small(self) -> None:
        assert validate_file_size(100) == 100

    def test_rejects_oversize(self) -> None:
        with pytest.raises(GuardrailViolation):
            validate_file_size(50 * 1024 * 1024)  # 50 MB
