#!/usr/bin/env python3
"""
ask.py -- ask the toolkit a free-form privacy/compliance question.

Runs the v1.2 RAG engine IN-PROCESS (no MCP server, no API key): retrieves the
most relevant framework rules from the Chroma index, asks the configured LLM to
answer using ONLY those rules, and validates every citation before printing.
A fabricated, mis-remembered, or low-confidence citation REFUSES the whole
answer rather than returning it.

Prereqs:
    * pip install -r requirements.txt   (chromadb, sentence-transformers, httpx)
    * a built vector index: python scripts/index_frameworks.py
    * Ollama running with the model in PCT_LLM_MODEL (default mistral:7b-instruct)

Usage:
    python scripts/ask.py "Which NIST CSF 2.0 subcategories cover incident notification?" --framework "NIST CSF"
    python scripts/ask.py "Where does the Danish DPA tighten GDPR's defaults?" --framework "Danish DPA"
    python scripts/ask.py "What is the lawful basis for processing under GDPR?"   # all frameworks
    python scripts/ask.py "..." --k 8 --json answer.json

Framework names: GDPR | Danish DPA | NIST CSF | ISO 27701 (omit to search all).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Allow running as `python scripts/ask.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ask the toolkit a compliance question (grounded RAG + citation checks).")
    p.add_argument("question", help="The question to ask.")
    p.add_argument("--framework", default=None,
                   help='Scope retrieval to one framework (e.g. "NIST CSF"). Omit to search all.')
    p.add_argument("--k", type=int, default=6, help="How many rules to retrieve (default 6).")
    p.add_argument("--json", dest="json_out", default=None, help="Also write the full result JSON here.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    from src.rag.embeddings import SentenceTransformerEmbedder
    from src.rag.vector_store import VectorStore
    from src.rag.engine import answer, RAGRefusal
    from src.llm.client import OllamaClient

    vs = VectorStore(embedder=SentenceTransformerEmbedder(), persist_dir=CONFIG.chroma_dir)
    llm = OllamaClient(
        model=CONFIG.llm_model,
        base_url=CONFIG.ollama_base_url,
        timeout_s=CONFIG.request_timeout_s,
        num_predict=1024,   # bound generation so a slow model can't eat the timeout
    )

    scope = args.framework or "all frameworks"
    print(f"--- asking ({scope}, k={args.k}): {args.question}")
    print("    (retrieving rules, then one CPU-Mistral call — first run loads the model)")

    t0 = time.time()
    try:
        a = answer(args.question, vector_store=vs, llm_client=llm,
                   framework=args.framework, k=args.k)
    except RAGRefusal as e:
        dt = time.time() - t0
        print(f"\nREFUSED ({dt:.1f}s): {e}", file=sys.stderr)
        print("(A refusal is the guardrail working: confidence below the floor, "
              "or a citation outside the retrieved/known set.)", file=sys.stderr)
        return 2
    dt = time.time() - t0

    print(f"\n+++ answered in {dt:.1f}s   confidence={a.confidence:.2f}\n")
    print(a.text.strip())
    print("\nCitations (verified against retrieved + known references):")
    if a.citations:
        for c in a.citations:
            print(f"  - {c.framework} | {c.reference}")
    else:
        print("  (none)")
    print(f"\nRetrieved rules shown to the model: {', '.join(a.retrieved_refs)}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "question": args.question,
            "framework": args.framework,
            "confidence": a.confidence,
            "answer": a.text,
            "citations": [{"framework": c.framework, "reference": c.reference} for c in a.citations],
            "retrieved_refs": a.retrieved_refs,
        }, indent=2), encoding="utf-8")
        print(f"\nFull result written to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
