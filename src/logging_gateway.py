"""
Logging Gateway — the only door to data.

Pattern (Cronk, *Strategic Privacy by Design*): every read or write of
sensitive data goes through this module. The audit log row is written and
fsync'd to disk BEFORE the data access happens. If the audit write fails,
the access fails — fail loud, never silent.

Usage:

    from src.logging_gateway import gateway

    with gateway.access(actor="mcp.get_article",
                        action="read",
                        resource="articles:GDPR Art. 6",
                        request_id=req_id) as ctx:
        row = ctx.fetch_one(
            "SELECT * FROM articles WHERE reference = ?",
            ("GDPR Art. 6",),
        )

Never bypass this. Direct `sqlite3.connect()` in any module other than
`db.py` (and only used here) is a security bug.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .config import CONFIG
from .db import connect


class GatewayError(RuntimeError):
    """Raised when the gateway cannot guarantee an audit-before-access."""


@dataclass
class AccessContext:
    """Handed back inside `gateway.access()`. The only legal way to read DB rows."""

    _conn: sqlite3.Connection
    request_id: str
    actor: str
    action: str
    resource: str

    def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        _assert_parameterized(sql, params)
        cur = self._conn.execute(sql, params)
        return cur.fetchone()

    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        _assert_parameterized(sql, params)
        cur = self._conn.execute(sql, params)
        return list(cur.fetchall())

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Write path. Returns lastrowid."""
        _assert_parameterized(sql, params)
        cur = self._conn.execute(sql, params)
        return cur.lastrowid or 0

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> None:
        _assert_parameterized(sql, seq_of_params[0] if seq_of_params else ())
        self._conn.executemany(sql, seq_of_params)


def _assert_parameterized(sql: str, params: Sequence[Any]) -> None:
    """Lightweight guard against accidental string interpolation in SQL.

    Not a full parser — that's what code review is for — but catches the
    common foot-gun of forgetting to use `?` placeholders.
    """
    if params and "?" not in sql and ":" not in sql:
        raise GatewayError(
            "SQL has parameters but no placeholders — refusing to execute. "
            "Use `?` parameter binding."
        )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _new_request_id() -> str:
    return uuid.uuid4().hex


class LoggingGateway:
    """
    Atomic audit-before-access. Writes are durable: fsync on the audit log
    file and a synchronous commit on the audit_log table.
    """

    def __init__(self, db_path: Path | None = None, audit_log_path: Path | None = None) -> None:
        self.db_path = db_path or CONFIG.db_path
        self.audit_log_path = audit_log_path or CONFIG.audit_log_path
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    # ----- audit -----

    def _write_audit(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        status: str,
        request_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """
        Write to BOTH the file-based audit log (fsync'd) AND the audit_log table.
        Either failing is a hard error — the access must not proceed.
        """
        ts = _utcnow_iso()
        meta_json = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)

        # 1) File log: line-protocol, fsync immediately.
        line = json.dumps(
            {
                "ts": ts,
                "actor": actor,
                "action": action,
                "resource": resource,
                "status": status,
                "request_id": request_id,
                "metadata": metadata or {},
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            with open(self.audit_log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            raise GatewayError(f"audit log write failed: {e}") from e

        # 2) DB log: synchronous insert.
        try:
            conn = connect()
            try:
                conn.execute(
                    """
                    INSERT INTO audit_log
                        (ts, actor, action, resource, status, request_id, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (ts, actor, action, resource, status, request_id, meta_json),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise GatewayError(f"audit DB insert failed: {e}") from e

    # ----- public API -----

    @contextmanager
    def access(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        request_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Iterator[AccessContext]:
        """
        Open an audited data-access scope.

        Order of operations:
            1. Validate args.
            2. Write audit row (file + DB) with status="ok".
               → If this fails, raise; no DB connection is opened.
            3. Open DB connection, yield AccessContext.
            4. On exception inside the `with` block, write a follow-up
               audit row with status="error" and re-raise.
        """
        if not actor or not action or not resource:
            raise GatewayError("actor, action, and resource are all required")

        req_id = request_id or _new_request_id()

        # Step 2 — audit BEFORE access.
        self._write_audit(
            actor=actor,
            action=action,
            resource=resource,
            status="ok",
            request_id=req_id,
            metadata=metadata,
        )

        # Step 3 — open the connection.
        conn = connect()
        try:
            yield AccessContext(
                _conn=conn,
                request_id=req_id,
                actor=actor,
                action=action,
                resource=resource,
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            # Loud failure — write a follow-up audit row.
            try:
                self._write_audit(
                    actor=actor,
                    action=action,
                    resource=resource,
                    status="error",
                    request_id=req_id,
                    metadata={"error_type": type(exc).__name__, "error": str(exc)[:500]},
                )
            except GatewayError:
                # If even the failure-audit fails, log to stderr and re-raise the original.
                print(f"[GATEWAY] FOLLOW-UP AUDIT FAILED for req {req_id}", file=sys.stderr)
            raise
        finally:
            conn.close()

    def deny(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        reason: str,
        request_id: str | None = None,
    ) -> None:
        """Record a denied access attempt without opening any data connection."""
        self._write_audit(
            actor=actor,
            action=action,
            resource=resource,
            status="denied",
            request_id=request_id or _new_request_id(),
            metadata={"reason": reason[:500]},
        )


# Module-level singleton — import this everywhere.
gateway = LoggingGateway()
