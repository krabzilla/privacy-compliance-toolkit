"""Logging gateway — audit-before-access + atomic semantics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _read_audit_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_access_writes_audit_first_then_yields(isolated_env: Path) -> None:
    from src.logging_gateway import gateway

    audit = isolated_env / "audit.log"

    with gateway.access(
        actor="test", action="read", resource="articles:1"
    ) as ctx:
        # Even before any DB call inside the with-block, the audit row must
        # already be on disk (audit-before-access).
        lines = _read_audit_lines(audit)
        assert len(lines) == 1
        assert lines[0]["actor"] == "test"
        assert lines[0]["action"] == "read"
        assert lines[0]["status"] == "ok"

        row = ctx.fetch_one("SELECT 1 AS x")
        assert row["x"] == 1


def test_failure_inside_block_writes_followup_audit(isolated_env: Path) -> None:
    from src.logging_gateway import gateway

    audit = isolated_env / "audit.log"

    with pytest.raises(RuntimeError, match="boom"):
        with gateway.access(actor="test", action="read", resource="articles:fail"):
            raise RuntimeError("boom")

    lines = _read_audit_lines(audit)
    # ok + error rows for the same request
    assert len(lines) == 2
    assert lines[0]["status"] == "ok"
    assert lines[1]["status"] == "error"
    assert lines[0]["request_id"] == lines[1]["request_id"]


def test_deny_records_without_opening_connection(isolated_env: Path) -> None:
    from src.logging_gateway import gateway

    gateway.deny(
        actor="test",
        action="read",
        resource="articles:blocked",
        reason="ssrf attempt",
    )
    lines = _read_audit_lines(isolated_env / "audit.log")
    assert any(line["status"] == "denied" for line in lines)


def test_unparameterised_sql_blocked(isolated_env: Path) -> None:
    from src.logging_gateway import GatewayError, gateway

    with gateway.access(actor="test", action="read", resource="x") as ctx:
        with pytest.raises(GatewayError):
            # Passing params but using f-string with no placeholders.
            ctx.fetch_one(f"SELECT 1 WHERE 'a' = 'b'", ("a",))
