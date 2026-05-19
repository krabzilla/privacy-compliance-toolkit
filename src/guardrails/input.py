"""
Input guardrails — Layer 1 of defense in depth.

Goals:
  - Block SSRF (private IPs, link-local, loopback, cloud metadata).
  - Refuse oversized payloads before reading them.
  - Sanitize text so it can't smuggle control characters or null bytes
    into downstream tools (DB, shell, LLM prompt).

All failures raise GuardrailViolation and are intended to be caught at the
edge (MCP/HTTP handler) so the response is a clean refusal, never a partial
result.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from ..config import CONFIG


class GuardrailViolation(ValueError):
    """Raised when an input fails a guardrail check."""


# ---------------------------------------------------------------------------
# URL / SSRF
# ---------------------------------------------------------------------------

_ALLOWED_SCHEMES = {"http", "https"}

# Cloud / orchestration metadata endpoints. Any resolution to these is rejected.
_METADATA_HOSTS = {
    "169.254.169.254",   # AWS / GCP / Azure / DO / OpenStack
    "metadata.google.internal",
    "metadata",          # k8s
    "fd00:ec2::254",     # AWS IMDS over IPv6
}


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url(url: str) -> str:
    """
    SSRF-safe URL validation. Returns the normalised URL.

    Note: this does NOT do a DNS resolution check on hostnames. For full SSRF
    safety in v1, resolve the hostname and re-check the resulting IP(s) before
    the actual fetch (and use the same socket — TOCTOU otherwise).
    """
    if not isinstance(url, str) or len(url) > 2048:
        raise GuardrailViolation("URL must be a string ≤ 2048 chars")

    parsed = urlparse(url.strip())
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise GuardrailViolation(f"scheme {parsed.scheme!r} not allowed (http/https only)")
    if not parsed.hostname:
        raise GuardrailViolation("URL has no hostname")

    host = parsed.hostname.lower()

    # Block known metadata hostnames outright.
    if host in _METADATA_HOSTS:
        raise GuardrailViolation(f"host {host!r} is a cloud-metadata endpoint")

    # If host is a literal IP, validate it directly.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None and _is_blocked_ip(ip):
        raise GuardrailViolation(f"IP {host!r} is in a blocked range (RFC1918 / loopback / link-local)")

    return parsed.geturl()


# ---------------------------------------------------------------------------
# File size
# ---------------------------------------------------------------------------


def validate_file_size(size_bytes: int) -> int:
    """Return size_bytes if under the cap; raise otherwise."""
    if size_bytes < 0:
        raise GuardrailViolation("negative size")
    cap = CONFIG.max_file_size_mb * 1024 * 1024
    if size_bytes > cap:
        raise GuardrailViolation(
            f"payload {size_bytes} bytes exceeds {CONFIG.max_file_size_mb} MB cap"
        )
    return size_bytes


# ---------------------------------------------------------------------------
# Text sanitization
# ---------------------------------------------------------------------------

# Control chars except \t \n \r
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Crude tag stripper — for XSS the right answer is templating, not regex. This
# is a defense-in-depth sweep in case raw text reaches an HTML sink.
_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_text(text: str, *, max_len: int = 10_000, strip_html: bool = False) -> str:
    """
    Normalise free-text input.

    - Reject null bytes (common SQL/path injection vector).
    - Strip control chars.
    - Enforce length cap.
    - Optionally strip HTML tags (defense-in-depth; rendering layer should
      still escape).
    """
    if not isinstance(text, str):
        raise GuardrailViolation("text must be a string")
    if "\x00" in text:
        raise GuardrailViolation("null byte in input")
    if len(text) > max_len:
        raise GuardrailViolation(f"text length {len(text)} exceeds {max_len}")
    cleaned = _CTRL_RE.sub("", text)
    if strip_html:
        cleaned = _TAG_RE.sub("", cleaned)
    return cleaned.strip()
