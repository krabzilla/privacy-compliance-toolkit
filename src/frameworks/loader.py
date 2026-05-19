"""
Framework CSV loader.

CSV schema (identical across frameworks):
    Category, Requirement, Body, Reference

The loader:
  1. Reads and hashes the file (source_hash → frameworks table).
  2. Validates each row (non-empty required fields, length caps).
  3. Inserts the framework row and bulk-inserts articles, all via the
     logging gateway so the load itself is audited.
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..config import CONFIG
from ..guardrails.input import GuardrailViolation, sanitize_text
from ..logging_gateway import gateway

REQUIRED_COLUMNS = ("Category", "Requirement", "Body", "Reference")


@dataclass(frozen=True)
class FrameworkSpec:
    name: str
    version: str
    filename: str   # relative to data/frameworks/


# Registry of frameworks shipped with the toolkit.
FRAMEWORK_REGISTRY: dict[str, FrameworkSpec] = {
    "GDPR": FrameworkSpec(name="GDPR", version="2016/679", filename="gdpr.csv"),
    # v1:
    # "Danish DPA": FrameworkSpec("Danish DPA", "2018", "danish_dpa.csv"),
    # "NIST CSF": FrameworkSpec("NIST CSF", "2.0", "nist_csf_2.csv"),
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _validate_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for i, row in enumerate(rows, start=2):  # start=2 because line 1 is header
        for col in REQUIRED_COLUMNS:
            if not row.get(col, "").strip():
                raise GuardrailViolation(f"row {i}: missing required column {col!r}")
        cleaned.append(
            {
                "Category": sanitize_text(row["Category"], max_len=200),
                "Requirement": sanitize_text(row["Requirement"], max_len=500),
                "Body": sanitize_text(row["Body"], max_len=10_000),
                "Reference": sanitize_text(row["Reference"], max_len=200),
            }
        )
    return cleaned


def load_framework_csv(spec: FrameworkSpec, *, csv_path: Path | None = None) -> int:
    """Load (or re-load) a framework CSV into the DB. Returns articles inserted."""
    path = (csv_path or (CONFIG.frameworks_dir / spec.filename)).resolve()
    if not path.exists():
        raise FileNotFoundError(f"framework CSV not found: {path}")

    src_hash = _sha256(path)

    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise GuardrailViolation(f"CSV missing required columns: {missing}")
        rows = _validate_rows(reader)

    if not rows:
        raise GuardrailViolation("CSV had no data rows")

    with gateway.access(
        actor="frameworks.loader",
        action="write",
        resource=f"frameworks:{spec.name}@{spec.version}",
        metadata={"path": str(path), "rows": len(rows), "source_hash": src_hash},
    ) as ctx:
        # Upsert-style: replace the framework row if (name, version) exists,
        # cascading the old articles via ON DELETE CASCADE.
        existing = ctx.fetch_one(
            "SELECT id FROM frameworks WHERE name = ? AND version = ?",
            (spec.name, spec.version),
        )
        if existing:
            ctx.execute("DELETE FROM frameworks WHERE id = ?", (existing["id"],))

        framework_id = ctx.execute(
            """
            INSERT INTO frameworks (name, version, source, source_hash)
            VALUES (?, ?, ?, ?)
            """,
            (spec.name, spec.version, str(path), src_hash),
        )

        ctx.executemany(
            """
            INSERT INTO articles
                (framework_id, category, requirement, body, reference, body_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    framework_id,
                    r["Category"],
                    r["Requirement"],
                    r["Body"],
                    r["Reference"],
                    _body_hash(r["Body"]),
                )
                for r in rows
            ],
        )

    return len(rows)
