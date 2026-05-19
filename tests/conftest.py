"""
Test fixtures. Each test gets its own DB + audit log under a tmp dir so
nothing leaks into the developer's real data/.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """
    Point all PCT_* paths at tmp_path and reload modules that captured
    CONFIG at import time. Returns the temp data dir.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Copy schema in
    schema_src = Path(__file__).resolve().parent.parent / "data" / "schema.sql"
    (data_dir / "schema.sql").write_text(schema_src.read_text(encoding="utf-8"), encoding="utf-8")
    # Copy frameworks dir
    fw_src = Path(__file__).resolve().parent.parent / "data" / "frameworks"
    fw_dst = data_dir / "frameworks"
    fw_dst.mkdir()
    for f in fw_src.glob("*.csv"):
        (fw_dst / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

    monkeypatch.setenv("PCT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PCT_DB_PATH", str(data_dir / "toolkit.db"))
    monkeypatch.setenv("PCT_AUDIT_LOG_PATH", str(data_dir / "audit.log"))

    # Force a re-import so CONFIG picks up the new env vars.
    import src.config
    importlib.reload(src.config)
    import src.db
    importlib.reload(src.db)
    import src.logging_gateway
    importlib.reload(src.logging_gateway)
    import src.frameworks.loader
    importlib.reload(src.frameworks.loader)

    # Init schema in the fresh DB.
    from src.db import init_schema
    init_schema(data_dir / "schema.sql")

    return data_dir
