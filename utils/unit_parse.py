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
# The unit itself: 3-7 digits, optionally carrying one or two letters glued on
# either side, with a letter prefix allowed to join through a hyphen. All four
# shapes are real fleet numbers -- "F9121", "ML2432", "1002FT", "T-120" -- and
# dropping the letters returns a *different* unit rather than none, which is
# worse. Letters never cross a space, so "1136 LORISTON" stays 1136 and does
# not become "1136 LO".
_UNIT = r"[A-Za-z]{0,2}-?\d{3,7}[A-Za-z]{0,2}"
# "UNIT# 1216", "UNIT 1216", "TRUCK# 147085", "Unit: 001A"
_LABELLED = re.compile(rf"\b(?:UNIT|TRUCK)\b\s*[#:]?\s*({_UNIT})", re.IGNORECASE)
# A bare leading number: "1136 LORISTON...", "0822 // FRANCOIS...",
# "T-120 QUINTERO...". Anchored to the very start of the title.
_LEADING = re.compile(rf"^\s*[^\w]*({_UNIT})\b")


def parse_unit(title: str | None) -> str | None:
    """Best guess at the unit in `title`, or None. Never trust it blindly."""
    if not title:
        return None
    for pattern in (_SUB, _LABELLED, _LEADING):
        m = pattern.search(title)
        if m:
            return m.group(1)
    return None


def parse_unit_from_description(description: str | None) -> str | None:
    """Best guess at the unit in a group's About text, or None.

    Deliberately stricter than `parse_unit`: only the *labelled* forms count
    ("UNIT 1216", "TRUCK# 147085", "SUB x // y"). A description is free prose,
    so the bare-leading-number rule that works on titles would happily return a
    phone number, a year, a DOT number or a street address.
    """
    if not description:
        return None
    for pattern in (_SUB, _LABELLED):
        m = pattern.search(description)
        if m:
            return m.group(1)
    return None


def guess_unit(title: str | None, description: str | None = None) -> tuple[str | None, str]:
    """Unit guess plus where it came from: ("1216", "title"|"description"|"").

    The title wins when both carry a number -- it is the field the fleet keeps
    current, while an About text is often stale boilerplate from whenever the
    group was created. The source is surfaced to the admin so a guess pulled out
    of prose is visibly weaker than one off the title.
    """
    unit = parse_unit(title)
    if unit:
        return unit, "title"
    unit = parse_unit_from_description(description)
    if unit:
        return unit, "description"
    return None, ""


def title_names_unit(title: str | None, unit: str | None) -> bool:
    """True if `unit` is still printed in `title`, as a whole token.

    A narrower question than `parse_unit`: not "what unit does this title name?"
    but "is the unit already on file still on it?" — which needs no guessing, so
    it holds for whatever format the fleet invents next.

    That distinction retired a running truck on 2026-08-26. The title sweep
    decides a group is dead when its title stops naming a unit, and it read that
    off `parse_unit` alone; the group titled "T-120 QUINTERO, JOHN / ..." had its
    number printed right there, and only the regex could not read a hyphenated
    prefix. Consulting the stored unit first can only ever *prevent* a
    deactivation, so a format nobody anticipated now costs a missed re-file
    instead of a live group.

    Bounded by non-alphanumeric edges on both sides: "120" must not match inside
    "21203", and "1002" must not match inside "1002FT" — a title naming 1002FT
    is not a title naming 1002.
    """
    unit = (unit or "").strip()
    if not title or not unit:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(unit)}(?![A-Za-z0-9])",
                          title, re.IGNORECASE))


def looks_retired(title: str | None) -> bool:
    """True if the fleet has renamed the group to mark it dead.

    Titles like "INACTIVE - ANTON, ROGEL" or "this group has been moved.
    Please use another one" mean the unit lives in a different chat now.
    """
    if not title:
        return False
    return bool(re.search(r"\binactive\b|has been moved|do not use", title, re.IGNORECASE))
