"""
try_tools.py -- ad-hoc client for poking the running Privacy Compliance
Toolkit MCP server end-to-end. Calls every tool the server exposes and
prints the results so you can see the full v1 pipeline in action.

Prereqs:
    - the MCP server running on http://127.0.0.1:8765
    - the same PCT_MCP_API_KEY exported in THIS shell (it lives in the env;
      it is not stored on disk anywhere by this script)
    - (for ask_compliance) Ollama installed and a model pulled, e.g.
      `ollama pull mistral:7b-instruct`

Usage:
    export PCT_MCP_API_KEY="..."          # same key the server is using
    python scripts/try_tools.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Allow running from the repo root without installing the project.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastmcp import Client  # noqa: E402


SERVER_URL = "http://127.0.0.1:8765/mcp/"


def _pretty(result) -> str:
    """Best-effort pretty-print of a tool's response.

    Tool results come back as a Content list; we want the JSON inside the
    first text chunk, formatted nicely so the toolkit's structured envelope
    (`ok`, `request_id`, ...) is easy to read.
    """
    content = getattr(result, "content", None) or []
    if content:
        first = content[0]
        text = getattr(first, "text", None)
        if text:
            try:
                return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except Exception:
                return text
    return repr(result)


def _hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


async def main() -> int:
    key = os.environ.get("PCT_MCP_API_KEY")
    if not key:
        print(
            "ERROR: PCT_MCP_API_KEY is not set in this shell. "
            "Export it first (same value the server is using).",
            file=sys.stderr,
        )
        return 1

    print(f"--- connecting to {SERVER_URL}")
    async with Client(SERVER_URL, auth=key) as client:
        tools = await client.list_tools()
        print(f"--- {len(tools)} tools available: {[t.name for t in tools]}")

        # 1. list_frameworks -- proves the server, auth, gateway, and DB all work.
        _hr("1. list_frameworks  (read SQLite via the audited gateway)")
        print(_pretty(await client.call_tool("list_frameworks", {})))

        # 2. get_article -- direct article fetch with citation verification.
        _hr("2. get_article('GDPR', 'GDPR Art. 6')")
        print(_pretty(await client.call_tool(
            "get_article", {"framework": "GDPR", "reference": "GDPR Art. 6"}
        )))

        # 3. search_frameworks -- keyword (LIKE) search across all four frameworks.
        _hr("3. search_frameworks('consent', limit=3)")
        print(_pretty(await client.call_tool(
            "search_frameworks", {"query": "consent", "limit": 3}
        )))

        # 4. semantic_search -- v1.1; vector retrieval across all frameworks.
        _hr("4. semantic_search('lawful basis for processing personal data', k=3)")
        print(_pretty(await client.call_tool(
            "semantic_search",
            {"query": "lawful basis for processing personal data", "k": 3},
        )))

        # 5. ask_compliance -- v1.2; full RAG with citation enforcement.
        _hr("5. ask_compliance('What is the lawful basis for processing under GDPR?')")
        print("(Ollama generates locally; first call after a fresh pull can take "
              "30-60 s while the model loads into RAM.)")
        print(_pretty(await client.call_tool(
            "ask_compliance",
            {"question": "What is the lawful basis for processing under GDPR?"},
        )))

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
