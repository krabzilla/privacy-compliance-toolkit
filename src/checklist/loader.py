"""
Notice-checklist loader.

Parses a privacy-notice requirement set (YAML) into validated, immutable
`NoticeRequirement` records and exposes applicability filtering against a
declared org profile.

Usage flow (mirrors the YAML header):
    1. Load the checklist:           cl = load_checklist()
    2. Declare the org's facts:      profile = {"data_collected_directly",
                                                "transfers_outside_eea"}
    3. Keep what applies:            reqs = cl.applicable(profile)
                                     -> every `always` requirement, plus any
                                        whose `applies_when` is in the profile.
       A requirement whose condition is absent is N/A -- it is not a gap.

Validation is deliberately strict and fail-loud (ChecklistError), in the same
spirit as the framework CSV loader: a malformed checklist should refuse to
load, not silently grade a policy against a half-parsed rule set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import CONFIG

# The closed set of facts an org may declare. Kept in code as the canonical
# vocabulary; the YAML's own `condition_vocabulary` is validated against it on
# load so the two cannot silently drift apart.
CONDITION_VOCABULARY: frozenset[str] = frozenset(
    {
        "always",
        "data_collected_directly",
        "data_collected_indirectly",
        "legal_basis_includes_legitimate_interest",
        "legal_basis_includes_consent",
        "transfers_outside_eea",
        "automated_decision_making_present",
        "special_category_data",
        "controller_outside_eu",
        "dpo_appointed",
        "cpr_processed",
    }
)

_VALID_CATEGORIES = frozenset({"mandatory", "conditional", "recommended"})

# Default location of the shipped GDPR notice checklist.
DEFAULT_CHECKLIST_PATH = CONFIG.data_dir / "checklists" / "gdpr_notice_requirements.yaml"


class ChecklistError(RuntimeError):
    """A checklist file failed to parse or violated the schema. Fail loud."""


@dataclass(frozen=True)
class NoticeRequirement:
    """One disclosure a privacy notice must (or should) make."""

    id: str
    title: str
    gdpr_articles: tuple[str, ...]
    category: str               # mandatory | conditional | recommended
    applies_when: str           # a member of CONDITION_VOCABULARY
    requirement: str            # rich description -- THIS is what gets embedded
    verifier_question: str      # single yes/no -> the LLM verification prompt
    positive_indicators: tuple[str, ...] = ()
    fix: str = ""
    national_law: str | None = None  # e.g. Danish DPA §11, where present

    @property
    def reference(self) -> str:
        """A human-facing citation string, e.g. 'GDPR Art. 13(1)(a)' or, for the
        Danish overlay, 'GDPR Art. 6, 87 + Databeskyttelsesloven §11'."""
        arts = ", ".join(f"GDPR Art. {a}" for a in self.gdpr_articles)
        if self.national_law:
            return f"{arts} + {self.national_law}" if arts else self.national_law
        return arts

    @property
    def is_always(self) -> bool:
        return self.applies_when == "always"


@dataclass(frozen=True)
class Checklist:
    """A loaded, validated notice checklist."""

    meta: dict
    condition_vocabulary: frozenset[str]
    requirements: tuple[NoticeRequirement, ...] = field(default_factory=tuple)

    @property
    def framework(self) -> str:
        return self.meta.get("framework", "GDPR notice")

    @property
    def jurisdiction(self) -> str:
        return self.meta.get("jurisdiction", "EU/EEA")

    def validate_profile(self, profile: set[str]) -> None:
        """Raise if the caller declared a fact outside the closed vocabulary."""
        unknown = set(profile) - self.condition_vocabulary
        if unknown:
            raise ChecklistError(
                f"unknown org-profile condition(s): {sorted(unknown)}; "
                f"valid conditions are {sorted(self.condition_vocabulary)}"
            )

    def applicable(self, profile: set[str] | None = None) -> list[NoticeRequirement]:
        """Requirements that apply given the declared org profile.

        Keeps every `always` requirement, plus any conditional requirement
        whose `applies_when` fact is present in the profile. `always` is always
        implied, so callers need not include it.
        """
        profile = set(profile or set())
        self.validate_profile(profile)
        active = profile | {"always"}
        return [r for r in self.requirements if r.applies_when in active]

    def not_applicable(self, profile: set[str] | None = None) -> list[NoticeRequirement]:
        """The complement of applicable() -- requirements filtered out as N/A."""
        keep = {r.id for r in self.applicable(profile)}
        return [r for r in self.requirements if r.id not in keep]


def _require(d: dict, key: str, ctx: str) -> object:
    if key not in d or d[key] in (None, "", [], ()):
        raise ChecklistError(f"{ctx}: missing required field {key!r}")
    return d[key]


def _as_str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    raise ChecklistError(f"expected a string or list, got {type(value).__name__}")


def load_checklist(path: str | Path | None = None) -> Checklist:
    """Load and validate a notice checklist from YAML.

    Raises ChecklistError on a missing file, unparseable YAML, or any schema
    violation (missing fields, unknown condition, bad category).
    """
    try:
        import yaml  # lazy -- only the notice path needs PyYAML
    except ImportError as e:  # pragma: no cover
        raise ChecklistError("PyYAML not installed; run `pip install pyyaml`") from e

    p = Path(path or DEFAULT_CHECKLIST_PATH).resolve()
    if not p.exists():
        raise ChecklistError(f"checklist file not found: {p}")

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ChecklistError(f"checklist YAML failed to parse: {e}") from e
    if not isinstance(raw, dict):
        raise ChecklistError("checklist root must be a mapping")

    meta = raw.get("meta") or {}
    if not isinstance(meta, dict):
        raise ChecklistError("'meta' must be a mapping")

    # The file declares its own vocabulary; validate it against the canonical
    # set so a typo in the YAML can't widen the closed condition set.
    file_vocab = set(_as_str_tuple(raw.get("condition_vocabulary")))
    if not file_vocab:
        raise ChecklistError("'condition_vocabulary' is required and must be non-empty")
    rogue = file_vocab - CONDITION_VOCABULARY
    if rogue:
        raise ChecklistError(
            f"checklist declares condition(s) not in the canonical vocabulary: "
            f"{sorted(rogue)}"
        )

    raw_reqs = raw.get("requirements")
    if not isinstance(raw_reqs, list) or not raw_reqs:
        raise ChecklistError("'requirements' must be a non-empty list")

    requirements: list[NoticeRequirement] = []
    seen_ids: set[str] = set()
    for entry in raw_reqs:
        if not isinstance(entry, dict):
            raise ChecklistError("each requirement must be a mapping")
        rid = str(_require(entry, "id", "requirement"))
        ctx = f"requirement {rid!r}"
        if rid in seen_ids:
            raise ChecklistError(f"duplicate requirement id {rid!r}")
        seen_ids.add(rid)

        category = str(_require(entry, "category", ctx))
        if category not in _VALID_CATEGORIES:
            raise ChecklistError(
                f"{ctx}: category must be one of {sorted(_VALID_CATEGORIES)}, "
                f"got {category!r}"
            )
        applies_when = str(_require(entry, "applies_when", ctx))
        if applies_when not in file_vocab:
            raise ChecklistError(
                f"{ctx}: applies_when {applies_when!r} is not in the checklist's "
                f"condition_vocabulary"
            )

        requirements.append(
            NoticeRequirement(
                id=rid,
                title=str(_require(entry, "title", ctx)),
                gdpr_articles=_as_str_tuple(_require(entry, "gdpr_articles", ctx)),
                category=category,
                applies_when=applies_when,
                requirement=str(_require(entry, "requirement", ctx)).strip(),
                verifier_question=str(_require(entry, "verifier_question", ctx)).strip(),
                positive_indicators=_as_str_tuple(entry.get("positive_indicators")),
                fix=str(entry.get("fix", "") or "").strip(),
                national_law=(
                    str(entry["national_law"]).strip()
                    if entry.get("national_law")
                    else None
                ),
            )
        )

    return Checklist(
        meta=meta,
        condition_vocabulary=frozenset(file_vocab),
        requirements=tuple(requirements),
    )
