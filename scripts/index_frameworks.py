#!/usr/bin/env python3
"""
Index every loaded framework into ChromaDB.

Reads from data/toolkit.db (via the gateway, so the read itself is audited)
and writes per-framework collections to PCT_CHROMA_DIR (default
data/chroma/). Idempotent: re-running replaces existing collections.

Usage:
    python scripts/load_frameworks.py    # populate SQLite first
    python scripts/index_frameworks.py   # then build the vector index
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/index_frameworks.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG
from src.logging_gateway import gateway
from src.rag import SentenceTransformerEmbedder, VectorStore


def main() -> int:
    vs = VectorStore(
        embedder=SentenceTransformerEmbedder(),
        persist_dir=CONFIG.chroma_dir,
    )

    # Pull the framework list + their articles via the gateway. The actual
    # Chroma upsert below opens its own (nested) gateway scope per framework
    # so each framework's index build gets its own audit row.
    with gateway.access(
        actor="scripts.index_frameworks",
        action="read",
        resource="frameworks:all",
    ) as ctx:
        frameworks = ctx.fetch_all(
            "SELECT id, name FROM frameworks ORDER BY name"
        )
        if not frameworks:
            print(
                "no frameworks loaded; run `python scripts/load_frameworks.py` first",
                file=sys.stderr,
            )
            return 1

        # Snapshot the articles per framework while inside the read scope so
        # the SQLite cursor lifecycle is contained.
        per_fw: dict[str, list[dict]] = {}
        for fw in frameworks:
            rows = ctx.fetch_all(
                """SELECT reference, category, requirement, body
                   FROM articles WHERE framework_id = ?
                   ORDER BY id""",
                (fw["id"],),
            )
            per_fw[fw["name"]] = [dict(r) for r in rows]

    # Now write to Chroma -- each framework gets its own gateway-audited write
    # scope (handled inside vs.upsert_articles).
    total = 0
    for name, articles in per_fw.items():
        print(f"indexing {name}: {len(articles)} articles ...")
        n = vs.upsert_articles(name, articles)
        print(f"  -> {n} embedded + indexed")
        total += n

    print(f"done. {total} articles indexed across {len(per_fw)} frameworks "
          f"into {CONFIG.chroma_dir}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
