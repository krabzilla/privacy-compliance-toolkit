"""LLMClient tests -- FakeLLMClient behaviours + JSON response parsing."""
from __future__ import annotations

import pytest

from src.llm import Citation, FakeLLMClient, LLMError, OllamaClient, Response
from src.llm.client import _response_from_dict


class TestFakeLLMClient:
    def test_responses_consumed_in_order(self) -> None:
        a = Response(text="first", citations=[], confidence=0.9)
        b = Response(text="second", citations=[], confidence=0.8)
        fc = FakeLLMClient(responses=[a, b])
        assert fc.complete("p1").text == "first"
        assert fc.complete("p2").text == "second"

    def test_calls_are_captured(self) -> None:
        fc = FakeLLMClient(responses=[Response(text="x")])
        fc.complete("hello world")
        assert fc.calls == ["hello world"]

    def test_exhaustion_raises_llm_error(self) -> None:
        fc = FakeLLMClient(responses=[Response(text="only one")])
        fc.complete("p1")
        with pytest.raises(LLMError):
            fc.complete("p2")

    def test_factory_mode(self) -> None:
        fc = FakeLLMClient(
            factory=lambda p: Response(text=f"got {len(p)} chars", confidence=0.9)
        )
        out = fc.complete("hello")
        assert out.text == "got 5 chars"

    def test_requires_exactly_one_of_responses_or_factory(self) -> None:
        with pytest.raises(ValueError):
            FakeLLMClient()
        with pytest.raises(ValueError):
            FakeLLMClient(
                responses=[Response(text="x")],
                factory=lambda p: Response(text="y"),
            )


class TestOllamaClient:
    def test_construct_does_not_call_network(self) -> None:
        # Constructing must not require httpx to be installed and must not
        # make a network call. The first complete() is the only thing that
        # might hit the wire.
        oc = OllamaClient(model="mistral:7b", base_url="http://127.0.0.1:11434")
        assert oc.model == "mistral:7b"
        assert oc.base_url == "http://127.0.0.1:11434"

    def test_trailing_slash_stripped_from_base_url(self) -> None:
        oc = OllamaClient(model="m", base_url="http://x/")
        assert oc.base_url == "http://x"


class TestResponseFromDict:
    def test_happy_path(self) -> None:
        r = _response_from_dict({
            "answer": "OK",
            "citations": [{"framework": "GDPR", "reference": "GDPR Art. 6"}],
            "confidence": 0.85,
        })
        assert r.text == "OK"
        assert r.citations == [Citation(framework="GDPR", reference="GDPR Art. 6")]
        assert r.confidence == 0.85

    def test_empty_dict_is_lenient(self) -> None:
        # Empty object -> defaults; engine will refuse via confidence floor.
        r = _response_from_dict({})
        assert r.text == ""
        assert r.citations == []
        assert r.confidence == 0.0

    @pytest.mark.parametrize("bad", [
        "not-a-dict",
        {"answer": 123},
        {"answer": "x", "citations": "not-a-list"},
        {"answer": "x", "citations": [{"framework": "GDPR"}]},   # missing reference
        {"answer": "x", "citations": [{"reference": "X"}]},      # missing framework
        {"answer": "x", "citations": ["not-an-object"]},
        {"answer": "x", "confidence": "high"},                   # not a number
        {"answer": "x", "confidence": 1.5},                      # out of [0,1]
        {"answer": "x", "confidence": -0.1},
    ])
    def test_drift_raises_llm_error(self, bad) -> None:
        with pytest.raises(LLMError):
            _response_from_dict(bad)
