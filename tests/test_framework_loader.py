"""End-to-end-ish: load GDPR CSV, verify articles round-trip via the gateway."""
from __future__ import annotations

from pathlib import Path


def test_load_gdpr_csv_full(isolated_env: Path) -> None:
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    spec = FRAMEWORK_REGISTRY["GDPR"]
    n = load_framework_csv(spec)
    assert n == 99

    with gateway.access(actor="test", action="read", resource="frameworks:GDPR") as ctx:
        row = ctx.fetch_one("SELECT COUNT(*) AS c FROM articles")
        assert row["c"] == 99

        # Spot-check Art. 6
        art6 = ctx.fetch_one(
            "SELECT * FROM articles WHERE reference = ?", ("GDPR Art. 6",)
        )
        assert art6 is not None
        assert "lawful" in art6["body"].lower()


def test_reload_is_idempotent(isolated_env: Path) -> None:
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    spec = FRAMEWORK_REGISTRY["GDPR"]
    load_framework_csv(spec)
    load_framework_csv(spec)  # second load should replace, not duplicate

    with gateway.access(actor="test", action="read", resource="frameworks:GDPR") as ctx:
        row = ctx.fetch_one("SELECT COUNT(*) AS c FROM articles")
        assert row["c"] == 99
