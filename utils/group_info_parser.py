from __future__ import annotations

import re

UNIT_PATTERNS = [
    re.compile(r"(?i)\bunit\s*[#:\-]?\s*(\d{2,6})\b"),
    re.compile(r"(?i)\btruck\s*[#:\-]?\s*(\d{2,6})\b"),
    re.compile(r"(?i)\btrk\s*[#:\-]?\s*(\d{2,6})\b"),
]
LEADING_NUMBER = re.compile(r"^\s*(\d{2,6})\b")

TRAILER_HINTS = re.compile(r"(?i)\btrailer\b|\btrl\s*[#:]?")
NOISE_HINTS = re.compile(r"(?i)\bphone|\bvin\b|\baddress|\bdescription")
NAMES_LABEL = re.compile(r"(?i)^\s*names?\s*[:#\-]?\s*(.+)$")
PAREN_SUFFIX = re.compile(r"\s*\([A-Za-z/]{1,5}\)\s*$")
HAS_LETTERS_3 = re.compile(r"[A-Za-z]{3,}")


def _strip_paren_suffix(s: str) -> str:
    while True:
        new = PAREN_SUFFIX.sub("", s).strip()
        if new == s:
            return new
        s = new


def _find_unit(text: str, allow_leading_number: bool) -> str | None:
    for line in text.splitlines():
        if TRAILER_HINTS.search(line) and not any(p.search(line) for p in UNIT_PATTERNS):
            continue
        for p in UNIT_PATTERNS:
            m = p.search(line)
            if m:
                return m.group(1)
    if allow_leading_number:
        m = LEADING_NUMBER.match(text)
        if m:
            return m.group(1)
    return None


def _find_names_line(text: str) -> str | None:
    for line in text.splitlines():
        m = NAMES_LABEL.match(line)
        if m:
            return m.group(1)
    for line in text.splitlines():
        s = line.strip()
        if not s or "/" not in s:
            continue
        if any(p.search(s) for p in UNIT_PATTERNS) or TRAILER_HINTS.search(s) or NOISE_HINTS.search(s):
            continue
        if not HAS_LETTERS_3.search(s):
            continue
        return s
    return None


def _names_from_title(title: str, unit: str | None) -> str | None:
    if not title:
        return None
    t = title
    if unit:
        t = re.sub(
            r"(?i)\b(?:unit|truck|trk)\s*[#:\-]?\s*" + re.escape(unit) + r"\b",
            "",
            t,
            count=1,
        )
        t = re.sub(r"^\s*" + re.escape(unit) + r"\b", "", t)
    t = t.strip(" -–—,/:;\t")
    return t if "/" in t and HAS_LETTERS_3.search(t) else None


def _split_drivers(names_raw: str) -> list[str]:
    cleaned = _strip_paren_suffix(names_raw)
    parts = re.split(r"\s*/+\s*", cleaned)
    out: list[str] = []
    for part in parts:
        p = _strip_paren_suffix(part.strip(" -–—,;:\t"))
        if len(p) < 3 or not HAS_LETTERS_3.search(p) or p.endswith("..."):
            continue
        out.append(p)
    return out[:2]


def extract_unit_and_names(title: str | None, description: str | None) -> tuple[str | None, list[str]]:
    """Best-effort extraction of (unit_number, [driver_name, ...]) from chat title + description.

    Returns (None, []) when nothing usable was found. Caller decides whether to propose or fall back.
    """
    title = (title or "").strip()
    description = (description or "").strip()

    unit = _find_unit(description, allow_leading_number=False)
    if not unit:
        unit = _find_unit(title, allow_leading_number=True)

    names_raw = _find_names_line(description) if description else None
    if not names_raw:
        names_raw = _names_from_title(title, unit)

    drivers = _split_drivers(names_raw) if names_raw else []
    return unit, drivers
