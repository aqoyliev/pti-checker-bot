"""Pull a unit number out of a group title.

Deliberately conservative. Measured against all 158 groups where the stored
unit_number was already known, a naive "first 3-6 digit run" regex scored
79.5%, and six titles yielded a *different valid unit* rather than nothing:

    "UNIT 1339 LAPLANTE..."        stored unit 1235
    "Unit 1232 MARCANO..."         stored unit 1264
    "1275 - NEGRON..."             stored unit 2003
    "1285/ ALTIDOR..."             stored unit 218779
    "SUB 588197 // 212566 FARAH"   stored unit 212566
    "SUB 429819 /// 728443 - ..."  stored unit 728443

A wrong unit silently misattributes inspections, which is worse than no unit
at all -- so this returns a *suggestion* that a human confirms, never a value
written straight to the database. The SUB cases are handled explicitly; the
rest is why the onboarding flow asks an admin to approve.
"""
from __future__ import annotations

import re

# "SUB <sublease number> // <real unit>" -- the real unit is the second number.
_SUB = re.compile(r"\bSUB\b\s*#?\s*\d{3,7}\s*[/|-]+\s*(\d{3,7})", re.IGNORECASE)
# "UNIT# 1216", "UNIT 1216", "TRUCK# 147085", "Unit: 001A"
_LABELLED = re.compile(r"\b(?:UNIT|TRUCK)\b\s*[#:]?\s*(\d{3,7})", re.IGNORECASE)
# A bare leading number: "1136 LORISTON...", "0822 // FRANCOIS..."
_LEADING = re.compile(r"^\s*[^\w]*(\d{3,7})\b")


def parse_unit(title: str | None) -> str | None:
    """Best guess at the unit in `title`, or None. Never trust it blindly."""
    if not title:
        return None
    for pattern in (_SUB, _LABELLED, _LEADING):
        m = pattern.search(title)
        if m:
            return m.group(1)
    return None


def looks_retired(title: str | None) -> bool:
    """True if the fleet has renamed the group to mark it dead.

    Titles like "INACTIVE - ANTON, ROGEL" or "this group has been moved.
    Please use another one" mean the unit lives in a different chat now.
    """
    if not title:
        return False
    return bool(re.search(r"\binactive\b|has been moved|do not use", title, re.IGNORECASE))
