"""
Notice-checklist layer (v1.5).

Where `src/frameworks` loads the *full* text of a regulation into SQLite (all
99 GDPR articles, etc.), this package loads a curated **notice-requirement
checklist** -- the much smaller set of disclosures a public privacy NOTICE
must actually make under GDPR Arts. 12-14 (plus the Danish CPR overlay).

The distinction matters: most of GDPR imposes internal/operational duties
(ROPA, DPIA, cooperation with the supervisory authority) that never belong in
a public notice and must NOT be graded against one. Grading a notice against
the whole regulation is what produced the false "Art. 30/31/32/35 gap" noise
in v1.3. The checklist is the curated target set instead.

The checklist is versioned as DATA (a YAML file under data/checklists/), not
code, on purpose -- see the file header for the rationale.
"""
from __future__ import annotations

from .loader import (
    CONDITION_VOCABULARY,
    Checklist,
    ChecklistError,
    NoticeRequirement,
    load_checklist,
)

__all__ = [
    "CONDITION_VOCABULARY",
    "Checklist",
    "ChecklistError",
    "NoticeRequirement",
    "load_checklist",
]
