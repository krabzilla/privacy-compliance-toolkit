"""Notice-checklist loader tests -- parsing, validation, profile filtering."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("yaml")


SHIPPED = Path(__file__).resolve().parent.parent / "data" / "checklists" / "gdpr_notice_requirements.yaml"


def test_loads_shipped_checklist() -> None:
    from src.checklist import load_checklist

    cl = load_checklist(SHIPPED)
    assert len(cl.requirements) == 18
    assert cl.framework.startswith("GDPR")
    # every requirement carries the fields the analyzer relies on
    for r in cl.requirements:
        assert r.id and r.requirement and r.verifier_question
        assert r.gdpr_articles
        assert r.category in {"mandatory", "conditional", "recommended"}


def test_reference_string_includes_national_law() -> None:
    from src.checklist import load_checklist

    cl = load_checklist(SHIPPED)
    cpr = next(r for r in cl.requirements if r.id == "NOTICE-DK-CPR")
    assert "Databeskyttelsesloven" in cpr.reference
    assert cpr.reference.startswith("GDPR Art.")


def test_always_requirements_apply_with_empty_profile() -> None:
    from src.checklist import load_checklist

    cl = load_checklist(SHIPPED)
    applicable = cl.applicable(set())
    ids = {r.id for r in applicable}
    # all mandatory always-on disclosures present
    assert "NOTICE-CONTROLLER-IDENTITY" in ids
    assert "NOTICE-PURPOSES" in ids
    assert "NOTICE-RETENTION" in ids
    # conditional ones are NOT pulled in without their fact
    assert "NOTICE-INTL-TRANSFERS" not in ids
    assert "NOTICE-DK-CPR" not in ids
    assert all(r.applies_when == "always" for r in applicable)


def test_conditional_requirement_activates_with_fact() -> None:
    from src.checklist import load_checklist

    cl = load_checklist(SHIPPED)
    applicable = cl.applicable({"transfers_outside_eea", "cpr_processed"})
    ids = {r.id for r in applicable}
    assert "NOTICE-INTL-TRANSFERS" in ids
    assert "NOTICE-DK-CPR" in ids
    # and the complement is reported N/A
    na_ids = {r.id for r in cl.not_applicable({"transfers_outside_eea", "cpr_processed"})}
    assert "NOTICE-DPO-CONTACT" in na_ids
    assert "NOTICE-INTL-TRANSFERS" not in na_ids


def test_unknown_profile_condition_is_rejected() -> None:
    from src.checklist import ChecklistError, load_checklist

    cl = load_checklist(SHIPPED)
    with pytest.raises(ChecklistError, match="unknown org-profile condition"):
        cl.applicable({"not_a_real_condition"})


def test_missing_file_is_rejected() -> None:
    from src.checklist import ChecklistError, load_checklist

    with pytest.raises(ChecklistError, match="not found"):
        load_checklist("/no/such/checklist.yaml")


def test_malformed_checklist_is_rejected(tmp_path: Path) -> None:
    from src.checklist import ChecklistError, load_checklist

    bad = tmp_path / "bad.yaml"
    # requirement references a condition not in the declared vocabulary
    bad.write_text(
        "meta: {framework: GDPR}\n"
        "condition_vocabulary: [always]\n"
        "requirements:\n"
        "  - id: X\n"
        "    title: t\n"
        "    gdpr_articles: ['13']\n"
        "    category: mandatory\n"
        "    applies_when: data_collected_directly\n"
        "    requirement: r\n"
        "    verifier_question: q\n",
        encoding="utf-8",
    )
    with pytest.raises(ChecklistError, match="condition_vocabulary"):
        load_checklist(bad)
