"""
Database connection helper.

Two rules, no exceptions:
1. Every query is parameterized — no f-strings in SQL.
2. Every data-touching operation goes through `logging_gateway`, not directly through this module.

SQLite today, PostgreSQL tomorrow — only the connection function should need to change.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import CONFIG


def _ensure_dirs() -> None:
    CONFIG.db_path.parent.mkdir(parents=True, exist_ok=True)


def connect() -> sqlite3.Connection:
    """Open a SQLite connection with safe pragmas."""
    _ensure_dirs()
    conn = sqlite3.connect(CONFIG.db_path, timeout=CONFIG.request_timeout_s)
    conn.row_factory = sqlite3.Row
    # Safer defaults
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    """Context manager: commits on success, rolls back on exception."""
    conn = connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(schema_path: Path | None = None) -> None:
    """Apply schema.sql idempotently."""
    schema_path = schema_path or (CONFIG.data_dir / "schema.sql")
    sql = Path(schema_path).read_text(encoding="utf-8")
    with cursor() as cur:
        cur.executescript(sql)
