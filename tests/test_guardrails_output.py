"""Output guardrail tests — PII redaction, confidence, citation verification."""
from __future__ import annotations

import pytest

from src.guardrails.input import GuardrailViolation
from src.guardrails.output import (
    enforce_confidence,
    extract_citations,
    redact_pii,
    verify_citations,
)


class TestRedactPII:
    def test_email(self) -> None:
        out = redact_pii("contact us at alice@example.com today")
        assert "[PII:EMAIL]" in out.text
        assert out.counts["EMAIL"] == 1

    def test_cpr(self) -> None:
        out = redact_pii("subject 010190-1234 wrote in")
        assert "[PII:CPR_DK]" in out.text

    def test_iban(self) -> None:
        out = redact_pii("send to DK5000400440116243")
        assert "[PII:IBAN]" in out.text

    def test_clean_text_unchanged(self) -> None:
        out = redact_pii("nothing to redact here")
        assert out.text == "nothing to redact here"
        assert out.total == 0


class TestEnforceConfidence:
    def test_above_threshold(self) -> None:
        assert enforce_confidence(0.9, threshold=0.75) == 0.9

    def test_below_threshold(self) -> None:
        with pytest.raises(GuardrailViolation):
            enforce_confidence(0.5, threshold=0.75)

    def test_out_of_range(self) -> None:
        with pytest.raises(GuardrailViolation):
            enforce_confidence(1.5)


class TestCitations:
    def test_extracts_gdpr(self) -> None:
        cites = extract_citations("see GDPR Art. 6 and GDPR Article 32(1)(b)")
        assert len(cites) == 2

    def test_extracts_nist(self) -> None:
        cites = extract_citations("ref NIST CSF GV.OC-01 applies")
        assert "NIST CSF GV.OC-01" in cites

    def test_verify_passes_when_known(self) -> None:
        bad = verify_citations("GDPR Art. 6 says...", ["GDPR Art. 6"])
        assert bad == []

    def test_verify_flags_unknown(self) -> None:
        bad = verify_citations("see GDPR Art. 999", ["GDPR Art. 6"])
        assert "GDPR Art. 999" in bad
