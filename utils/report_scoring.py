"""Completeness scoring for the fleet PTI reports.

Pure functions only -- no DB, no network -- so the numbers printed on a report
can be unit-tested. ``scripts/fleet_report.py`` is the only caller.

The score is the one the fleet has already been shown (see the Aug 2026 gurman
reports), so it must not drift:

    85 pts  required areas actually filmed (8, or 9 when the under-hood check
            applies), pro-rated
     5 pts  the fire extinguisher storage area was shown
    10 pts  no specific sub-item was flagged "not visible" (-2 each, floored at 0)

A driver who filmed every area but never showed the extinguisher scores 95%.
The score is *not* the verdict: the bot's PASS/FAIL is decided only by whether
every required area was filmed, and the extinguisher never fails an inspection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

# The 8 areas every inspection must film. "Under hood" is the conditional 9th:
# the model is told to omit it from missing_areas when it was not filmed, so it
# only joins the required set once it actually appears in the footage.
REQUIRED_AREAS = (
    "Brake pads",
    "Lights",
    "Tires",
    "Mirrors",
    "Windshield",
    "Air lines",
    "Frame",
    "ABS lamp",
)
UNDER_HOOD = "Under hood"

# Class boundaries, by how many required areas went unfilmed.
COMPLETE, REAL, PARTIAL, NOT_A_PTI = "Complete", "Real", "Partial", "Not a PTI"


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _as_list(data: dict, key: str) -> list[str]:
    raw = data.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(x).strip() for x in raw if str(x).strip()]


def under_hood_filmed(data: dict) -> bool:
    """True when the optional under-hood check appears in the footage.

    It counts as filmed if the model cleared it or raised an issue about it;
    an un-filmed hood is simply absent from both, never in ``missing_areas``.
    """
    hood = _norm(UNDER_HOOD)
    for key in ("checked_clean", "issues"):
        for item in _as_list(data, key):
            if hood in _norm(item):
                return True
    return False


def missing_required(data: dict) -> list[str]:
    """The required areas the driver never filmed, canonically labelled."""
    canon = {_norm(a): a for a in REQUIRED_AREAS}
    seen, out = set(), []
    for item in _as_list(data, "missing_areas"):
        key = _norm(item)
        if key in canon and key not in seen:
            seen.add(key)
            out.append(canon[key])
    return out


def classify(n_missing: int) -> str:
    if n_missing == 0:
        return COMPLETE
    if n_missing <= 2:
        return REAL
    if n_missing <= 5:
        return PARTIAL
    return NOT_A_PTI


@dataclass(frozen=True)
class Score:
    """One inspection's scored coverage."""

    score: int          # 0-100 completeness
    filmed: int         # required areas actually filmed
    required: int       # 8, or 9 when the under-hood check applied
    missing: list[str]  # required areas not filmed
    fire_extinguisher: bool
    not_visible: list[str]

    @property
    def klass(self) -> str:
        return classify(len(self.missing))

    @property
    def is_real(self) -> bool:
        """A genuine walkaround: at most 2 of the required areas unfilmed."""
        return len(self.missing) <= 2


def score_inspection(result_json: str | dict | None) -> Score:
    """Score one ``pti_log.result_json`` payload.

    An unparseable or empty payload scores 0 with every area counted missing --
    a submission the pipeline could not read is not evidence of a walkaround.
    """
    data: dict = {}
    if isinstance(result_json, dict):
        data = result_json
    elif result_json:
        try:
            parsed = json.loads(result_json)
            if isinstance(parsed, dict):
                data = parsed
        except (ValueError, TypeError):
            data = {}

    required = len(REQUIRED_AREAS) + (1 if under_hood_filmed(data) else 0)
    missing = missing_required(data)
    if under_hood_filmed(data) is False and not data:
        missing = list(REQUIRED_AREAS)
    filmed = max(0, required - len(missing))

    fe = bool(data.get("fire_extinguisher_shown"))
    not_visible = _as_list(data, "what_was_not_visible")

    areas_pts = 85.0 * filmed / required if required else 0.0
    fe_pts = 5.0 if fe else 0.0
    detail_pts = max(0.0, 10.0 - 2.0 * len(not_visible))

    return Score(
        score=int(round(areas_pts + fe_pts + detail_pts)),
        filmed=filmed,
        required=required,
        missing=missing,
        fire_extinguisher=fe,
        not_visible=not_visible,
    )
