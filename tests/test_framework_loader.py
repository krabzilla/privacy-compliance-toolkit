"""End-to-end-ish: load each framework CSV, verify rows round-trip via the gateway."""
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
    load_framework_csv(spec)

    with gateway.access(actor="test", action="read", resource="frameworks:GDPR") as ctx:
        row = ctx.fetch_one("SELECT COUNT(*) AS c FROM articles")
        assert row["c"] == 99


def test_load_danish_dpa_csv_full(isolated_env: Path) -> None:
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    spec = FRAMEWORK_REGISTRY["Danish DPA"]
    n = load_framework_csv(spec)
    assert n == 25, f"expected 25 Danish DPA rows, got {n}"

    with gateway.access(actor="test", action="read", resource="frameworks:Danish DPA") as ctx:
        cpr = ctx.fetch_one(
            "SELECT * FROM articles WHERE reference = ?", ("Danish DPA § 11",)
        )
        assert cpr is not None
        assert "cpr" in cpr["body"].lower() or "civil registration" in cpr["body"].lower()

        age = ctx.fetch_one(
            "SELECT * FROM articles WHERE reference = ?", ("Danish DPA § 6",)
        )
        assert age is not None
        assert "13" in age["body"]


def test_load_nist_csf_csv_full(isolated_env: Path) -> None:
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    spec = FRAMEWORK_REGISTRY["NIST CSF"]
    n = load_framework_csv(spec)
    assert n == 106, f"expected 106 NIST CSF subcategories, got {n}"

    with gateway.access(actor="test", action="read", resource="frameworks:NIST CSF") as ctx:
        govoc = ctx.fetch_one(
            "SELECT * FROM articles WHERE reference = ?", ("NIST CSF GV.OC-01",)
        )
        assert govoc is not None
        assert "mission" in govoc["body"].lower()

        for ref in [
            "NIST CSF ID.AM-01",
            "NIST CSF PR.AA-01",
            "NIST CSF DE.CM-01",
            "NIST CSF RS.MA-01",
            "NIST CSF RC.RP-01",
        ]:
            r = ctx.fetch_one("SELECT * FROM articles WHERE reference = ?", (ref,))
            assert r is not None, f"missing expected subcategory {ref}"


def test_load_iso_27701_csv_full(isolated_env: Path) -> None:
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    spec = FRAMEWORK_REGISTRY["ISO 27701"]
    n = load_framework_csv(spec)
    assert n == 49, f"expected 49 ISO 27701 controls, got {n}"

    with gateway.access(actor="test", action="read", resource="frameworks:ISO 27701") as ctx:
        # Spot-check the lawful-basis control (the bridge to GDPR Art. 6)
        a722 = ctx.fetch_one(
            "SELECT * FROM articles WHERE reference = ?", ("ISO 27701 A.7.2.2",)
        )
        assert a722 is not None
        assert "lawful" in a722["body"].lower()

        # Spot-check one control from each major bucket (controllers + processors)
        for ref in [
            "ISO 27701 A.7.2.1",   # purpose
            "ISO 27701 A.7.3.1",   # obligations to principals (controller)
            "ISO 27701 A.7.4.1",   # limit collection
            "ISO 27701 A.7.5.1",   # transfer basis
            "ISO 27701 A.8.2.1",   # customer agreement (processor)
            "ISO 27701 A.8.5.6",   # subprocessor disclosure
        ]:
            r = ctx.fetch_one("SELECT * FROM articles WHERE reference = ?", (ref,))
            assert r is not None, f"missing expected ISO 27701 control {ref}"


def test_four_frameworks_loaded_together(isolated_env: Path) -> None:
    """The v1.0b acceptance test: all four frameworks live side-by-side."""
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    for name in ("GDPR", "Danish DPA", "NIST CSF", "ISO 27701"):
        load_framework_csv(FRAMEWORK_REGISTRY[name])

    with gateway.access(actor="test", action="read", resource="frameworks:*") as ctx:
        names = [r["name"] for r in ctx.fetch_all("SELECT name FROM frameworks ORDER BY name")]
        assert names == ["Danish DPA", "GDPR", "ISO 27701", "NIST CSF"]

        # Total = 99 (GDPR) + 25 (Danish) + 106 (NIST) + 49 (ISO) = 279
        total = ctx.fetch_one("SELECT COUNT(*) AS c FROM articles")["c"]
        assert total == 279, f"expected 279 articles across 4 frameworks, got {total}"


def test_danish_reload_is_idempotent(isolated_env: Path) -> None:
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    spec = FRAMEWORK_REGISTRY["Danish DPA"]
    load_framework_csv(spec)
    load_framework_csv(spec)

    with gateway.access(actor="test", action="read", resource="frameworks:Danish DPA") as ctx:
        c = ctx.fetch_one(
            """SELECT COUNT(*) AS c FROM articles a
               JOIN frameworks f ON f.id = a.framework_id
               WHERE f.name = ?""",
            ("Danish DPA",),
        )["c"]
        assert c == 25


def test_iso_27701_reload_is_idempotent(isolated_env: Path) -> None:
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    spec = FRAMEWORK_REGISTRY["ISO 27701"]
    load_framework_csv(spec)
    load_framework_csv(spec)

    with gateway.access(actor="test", action="read", resource="frameworks:ISO 27701") as ctx:
        c = ctx.fetch_one(
            """SELECT COUNT(*) AS c FROM articles a
               JOIN frameworks f ON f.id = a.framework_id
               WHERE f.name = ?""",
            ("ISO 27701",),
        )["c"]
        assert c == 49
