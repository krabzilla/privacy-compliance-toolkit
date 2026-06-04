"""
LLMClient implementations.

Design rules:
  * The LLMClient Protocol is the only contract the rag.engine consumes.
    Engine code never branches on "is this Ollama or OpenAI" -- swapping
    providers is a constructor change, not a control-flow change.

  * Responses are STRUCTURED. Plain-text reasoning lives in .text; citations
    live in .citations as Citation(framework, reference) records. The engine
    validates the structured list against the retrieved + known reference
    sets -- it does not regex-extract citations from prose. That is the
    v1.2 thesis closing the non-canonical-citation gap that v0.1 documented.

  * Implementations import their backend LAZILY. Constructing an
    OllamaClient does NOT require httpx to be installed.

  * Transport / parsing failures raise LLMError. The engine catches LLMError
    and converts it to a refusal -- the toolkit's contract is: any failure
    in the LLM path produces an explicit refusal, never a partial answer.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Protocol


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMError(RuntimeError):
    """Raised when the LLM call fails (transport, timeout, invalid response)."""


# ---------------------------------------------------------------------------
# Structured Response shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """One structured citation emitted by the LLM. The engine validates these."""

    framework: str
    reference: str


@dataclass(frozen=True)
class Response:
    """LLM output as expected by the engine. Always has these three fields."""

    text: str
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class LLMClient(Protocol):
    """The contract the rag.engine + rag.gap_analysis consume."""

    def complete(self, prompt: str) -> Response:
        """Structured Q&A response (answer / citations / confidence)."""
        ...

    def complete_json(self, prompt: str) -> dict:
        """Raw JSON response as a parsed dict. Use for callers (like
        rag.gap_analysis) whose JSON schema is NOT the Q&A shape.
        Implementations must still enforce that the response is valid JSON
        and raise LLMError on transport / parsing failure."""
        ...


# ---------------------------------------------------------------------------
# Production: Ollama HTTP
# ---------------------------------------------------------------------------


class OllamaClient:
    """
    Talks to a locally-running Ollama HTTP server via POST /api/chat.

    Uses Ollama's `format: "json"` mode to constrain the model to JSON output,
    which is the contract our prompts ask for. The JSON is parsed and validated
    against the expected shape (answer / citations / confidence); any deviation
    raises LLMError, which the engine converts to a refusal.

    httpx is imported lazily inside complete() so importing this module does
    not require the dependency. The request timeout uses the configured
    request_timeout_s so we never hang on a slow model.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        timeout_s: int = 30,
        num_predict: int | None = None,
        num_ctx: int | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        # Optional Ollama generation bounds. num_predict caps how many tokens
        # the model may GENERATE (our JSON replies are small, so capping this
        # stops a runaway model from eating the whole timeout). num_ctx sets the
        # context window. Both default to None -> Ollama's own defaults, so
        # existing behaviour is unchanged unless a caller opts in.
        self.num_predict = num_predict
        self.num_ctx = num_ctx

    def _call_and_parse(self, prompt: str) -> dict:
        """HTTP call + JSON parse. Shared by complete() and complete_json()."""
        try:
            import httpx  # lazy
        except ImportError as e:  # pragma: no cover
            raise LLMError("httpx not installed; run `pip install httpx`") from e

        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
        }
        options: dict = {}
        if self.num_predict is not None:
            options["num_predict"] = self.num_predict
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        if options:
            payload["options"] = options
        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                http_resp = client.post(url, json=payload)
                http_resp.raise_for_status()
                body = http_resp.json()
        except httpx.TimeoutException as e:
            raise LLMError(f"Ollama call timed out after {self.timeout_s}s") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama HTTP error: {e}") from e

        # Ollama puts the model's reply at body["message"]["content"] as a
        # JSON string (because we asked for format: "json").
        content = (body.get("message") or {}).get("content")
        if not content or not isinstance(content, str):
            raise LLMError("Ollama response missing message.content")
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"Ollama response not valid JSON: {e}") from e

    def complete(self, prompt: str) -> Response:
        """Structured Q&A response. Expects answer/citations/confidence schema."""
        return _response_from_dict(self._call_and_parse(prompt))

    def complete_json(self, prompt: str) -> dict:
        """Raw JSON dict -- for callers whose schema is NOT Q&A."""
        return self._call_and_parse(prompt)


# ---------------------------------------------------------------------------
# Test: deterministic in-memory client
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """
    Deterministic LLM stand-in for tests.

    Construct with either:
        FakeLLMClient(responses=[Response(...), Response(...)])
            -- consumed in order, IndexError if the test calls more than provided
        FakeLLMClient(factory=lambda prompt: Response(...))
            -- called with each prompt; lets a test inspect the prompt and
               return contextually-correct Responses

    Either way, no network, no model, fully deterministic.
    """

    def __init__(
        self,
        *,
        responses: list[Response] | None = None,
        factory: Callable[[str], Response] | None = None,
    ) -> None:
        if (responses is None) == (factory is None):
            raise ValueError("provide exactly one of `responses` or `factory`")
        self._responses = list(responses) if responses is not None else None
        self._factory = factory
        self._idx = 0
        self.calls: list[str] = []  # prompts captured for test assertions

    def complete(self, prompt: str) -> Response:
        """Structured Q&A path. Returns the canned Response as-is."""
        self.calls.append(prompt)
        if self._factory is not None:
            return self._factory(prompt)
        assert self._responses is not None  # for mypy
        if self._idx >= len(self._responses):
            raise LLMError(
                f"FakeLLMClient exhausted: test set up {len(self._responses)} "
                f"response(s) but the engine made {self._idx + 1} call(s)"
            )
        r = self._responses[self._idx]
        self._idx += 1
        return r

    def complete_json(self, prompt: str) -> dict:
        """Raw-JSON path. The canned Response.text is itself the JSON string
        the test wants the LLM to return; parse it on the way out so the same
        FakeLLMClient instance can serve both call paths."""
        resp = self.complete(prompt)
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"FakeLLMClient.complete_json: canned response.text is not "
                f"valid JSON: {e}"
            ) from e


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _response_from_dict(d: dict) -> Response:
    """Validate the parsed JSON against the contract; raise LLMError on drift."""
    if not isinstance(d, dict):
        raise LLMError("LLM response is not a JSON object")
    text = d.get("answer", "")
    if not isinstance(text, str):
        raise LLMError("LLM response 'answer' must be a string")
    raw_cites = d.get("citations", [])
    if not isinstance(raw_cites, list):
        raise LLMError("LLM response 'citations' must be a list")
    citations: list[Citation] = []
    for c in raw_cites:
        if not isinstance(c, dict):
            raise LLMError("each citation must be an object")
        fw = c.get("framework")
        ref = c.get("reference")
        if not isinstance(fw, str) or not isinstance(ref, str):
            raise LLMError("each citation must have string 'framework' and 'reference'")
        citations.append(Citation(framework=fw, reference=ref))
    conf = d.get("confidence", 0.0)
    if not isinstance(conf, (int, float)):
        raise LLMError("LLM response 'confidence' must be a number")
    if not (0.0 <= float(conf) <= 1.0):
        raise LLMError(f"confidence {conf} out of range [0, 1]")
    return Response(text=text, citations=citations, confidence=float(conf))
