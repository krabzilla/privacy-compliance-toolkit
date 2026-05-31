"""
LLM client wrapper -- provider-agnostic.

Two implementations live behind the LLMClient Protocol:

  * OllamaClient   the production default. Talks to a locally-running
                   Ollama HTTP server (see PCT_OLLAMA_BASE_URL). httpx is
                   lazily imported so tests that only use FakeLLMClient do
                   not pull in HTTP machinery.

  * FakeLLMClient  deterministic in-memory client for tests. Accepts either
                   a list of Response objects (consumed in order) or a
                   callable(prompt: str) -> Response.

Both expose complete(prompt: str) -> Response, where Response carries a
plain-text answer, a STRUCTURED list of citations, and a confidence score.
Structured citations are the v1.2 thesis: the engine never has to extract
citations from prose because the LLM emits them in a separate field that the
engine validates against the retrieved + known reference sets.
"""
from __future__ import annotations

from .client import (
    Citation,
    FakeLLMClient,
    LLMClient,
    LLMError,
    OllamaClient,
    Response,
)

__all__ = [
    "Citation",
    "FakeLLMClient",
    "LLMClient",
    "LLMError",
    "OllamaClient",
    "Response",
]
