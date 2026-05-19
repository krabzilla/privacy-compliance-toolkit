"""
Output guardrails -- Layer 4 (Layer 5 is human sign-off).

Goals:
  - Redact common PII patterns before returning text to a caller.
  - Refuse to return outputs below the confidence threshold.
  - Verify that every citation in the output corresponds to a real row in
    the loaded framework (citation-must-trace-back).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..config import CONFIG
from .input import GuardrailViolation


# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

# Conservative regex set. v1 replaces this with an NER model + locale-aware
# detectors (CPR numbers, IBANs by country, etc.).
#
# ORDER MATTERS -- most specific patterns first. PHONE is greedy and will eat
# a CPR or SSN if it runs ahead of them.
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")),
    ("CPR_DK", re.compile(r"\b\d{6}-\d{4}\b")),
    ("SSN_US", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("IPV4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("PHONE", re.compile(r"\b(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?){2,4}\d{2,4}\b")),
]


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def redact_pii(text: str) -> RedactionResult:
    """Replace matches with [PII:TYPE]. Returns redacted text + per-type counts."""
    if not isinstance(text, str):
        raise GuardrailViolation("text must be a string")

    counts: dict[str, int] = {}
    out = text
    for label, pat in _PII_PATTERNS:
        new, n = pat.subn(f"[PII:{label}]", out)
        if n:
            counts[label] = counts.get(label, 0) + n
        out = new
    return RedactionResult(text=out, counts=counts)


# ---------------------------------------------------------------------------
# Confidence threshold
# ---------------------------------------------------------------------------


def enforce_confidence(score: float, *, threshold: float | None = None) -> float:
    """Raise if score < threshold; return score otherwise."""
    floor = threshold if threshold is not None else CONFIG.confidence_threshold
    if not (0.0 <= score <= 1.0):
        raise GuardrailViolation(f"confidence {score} out of range [0,1]")
    if score < floor:
        raise GuardrailViolation(
            f"confidence {score:.3f} below threshold {floor:.3f} -- route to review queue"
        )
    return score


# ---------------------------------------------------------------------------
# Citation verification
# ---------------------------------------------------------------------------

_CITATION_RE = re.compile(
    r"\b(?:GDPR\s+Art(?:icle|\.)?\s+\d+(?:\([0-9a-z]+\))*"
    r"|Danish\s+DPA\s+\xa7\s*\d+"
    r"|NIST\s+CSF\s+[A-Z]{2}\.[A-Z]{2}-\d{2})\b",
    re.IGNORECASE,
)


def extract_citations(text: str) -> list[str]:
    return [m.group(0).strip() for m in _CITATION_RE.finditer(text or "")]


def verify_citations(text: str, known_references: Iterable[str]) -> list[str]:
    """Return citations in text that are NOT in known_references. Empty = all valid."""
    known = {r.strip().lower() for r in known_references}
    found = extract_citations(text)
    return [c for c in found if c.lower() not in known]
