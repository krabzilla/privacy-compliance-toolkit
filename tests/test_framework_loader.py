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


def test_load_danish_dpa_csv_full(isolated_env: Path) -> None:
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    spec = FRAMEWORK_REGISTRY["Danish DPA"]
    n = load_framework_csv(spec)
    assert n == 25, f"expected 25 Danish DPA rows, got {n}"

    with gateway.access(actor="test", action="read", resource="frameworks:Danish DPA") as ctx:
        # Spot-check § 11 (the CPR provision -- one of the Act's most distinctive)
        cpr = ctx.fetch_one(
            "SELECT * FROM articles WHERE reference = ?", ("Danish DPA § 11",)
        )
        assert cpr is not None
        assert "cpr" in cpr["body"].lower() or "civil registration" in cpr["body"].lower()

        # Spot-check § 6 (age of consent set to 13)
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
        # Spot-check GV.OC-01 (mission-aligned cybersecurity risk management)
        govoc = ctx.fetch_one(
            "SELECT * FROM articles WHERE reference = ?", ("NIST CSF GV.OC-01",)
        )
        assert govoc is not None
        assert "mission" in govoc["body"].lower()

        # Spot-check one from each function so a missing function would fail loudly
        for ref in [
            "NIST CSF ID.AM-01",  # Identify
            "NIST CSF PR.AA-01",  # Protect
            "NIST CSF DE.CM-01",  # Detect
            "NIST CSF RS.MA-01",  # Respond
            "NIST CSF RC.RP-01",  # Recover
        ]:
            r = ctx.fetch_one("SELECT * FROM articles WHERE reference = ?", (ref,))
            assert r is not None, f"missing expected subcategory {ref}"


def test_three_frameworks_loaded_together(isolated_env: Path) -> None:
    """The v1.0a acceptance test: all three frameworks live side-by-side."""
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    for name in ("GDPR", "Danish DPA", "NIST CSF"):
        load_framework_csv(FRAMEWORK_REGISTRY[name])

    with gateway.access(actor="test", action="read", resource="frameworks:*") as ctx:
        names = [r["name"] for r in ctx.fetch_all("SELECT name FROM frameworks ORDER BY name")]
        assert names == ["Danish DPA", "GDPR", "NIST CSF"]

        # Total = 99 + 25 + 106 = 230
        total = ctx.fetch_one("SELECT COUNT(*) AS c FROM articles")["c"]
        assert total == 230, f"expected 230 articles across 3 frameworks, got {total}"


def test_danish_reload_is_idempotent(isolated_env: Path) -> None:
    from src.frameworks import FRAMEWORK_REGISTRY, load_framework_csv
    from src.logging_gateway import gateway

    spec = FRAMEWORK_REGISTRY["Danish DPA"]
    load_framework_csv(spec)
    load_framework_csv(spec)

    with gateway.access(actor="test", action="read", resource="frameworks:Danish DPA") as ctx:
        # 25 (Danish only) -- the GDPR-cascade behaviour is already covered above
        c = ctx.fetch_one(
            """SELECT COUNT(*) AS c FROM articles a
               JOIN frameworks f ON f.id = a.framework_id
               WHERE f.name = ?""",
            ("Danish DPA",),
        )["c"]
        assert c == 25
