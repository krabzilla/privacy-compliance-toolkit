"""Initialise the SQLite database from data/schema.sql. Idempotent."""
from __future__ import annotations

import sys
from pathlib import Path

# Allow `python scripts/init_db.py` from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import CONFIG  # noqa: E402
from src.db import init_schema  # noqa: E402


def main() -> None:
    print(f"Initialising DB at: {CONFIG.db_path}")
    schema = CONFIG.data_dir / "schema.sql"
    init_schema(schema)
    print(f"Schema applied from: {schema}")


if __name__ == "__main__":
    main()
