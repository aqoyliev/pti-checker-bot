"""Finding and normalizing phone numbers in free text.

Split out of ``utils/phone_lookup`` so that reading a number is separate from
*resolving* one. Resolving needs a Telethon user session, an API id and a
rate-limit budget; reading a group's About text needs none of that, and two
callers only ever want the reading half — ``utils/driver_names`` pairing names
to numbers, and the web panel showing an admin who a unit's drivers are.

Pure: no config, no network, no database.
"""
from __future__ import annotations

import re

# The fleet is US-only, so a bare 10-digit number is a US number. Numbers are
# typed by hand ("786-488-2619"), and Telegram silently resolves nothing for a
# number without a country code — indistinguishable from "not on Telegram".
DEFAULT_COUNTRY_CODE = "1"

# A run of 10-15 digits with the usual separators. Deliberately stricter than
# normalize_phone: a group's About text is free prose full of numbers -- unit
# numbers, trailer numbers, years, dollar amounts -- and the same "descriptions
# are parsed more strictly than titles" reasoning that governs unit parsing
# applies here. Ten digits is the shortest thing that is unambiguously a phone.
#
# The optional opening bracket is not decoration: the match is shown to a person
# as "(561) 674-7866", and starting a digit later leaves a stray "(" behind on
# the line it was cut from.
PHONE_RE = re.compile(r"\(?\+?\d[\d\s().\-]{8,18}\d")


def normalize_phone(raw: str, default_country: str = DEFAULT_COUNTRY_CODE) -> str | None:
    """'786-488-2619' -> '+17864882619'. None when it cannot be a phone number.

    An explicit '+' is trusted as already-international; a bare 10-digit number
    gets the default country code.
    """
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not 7 <= len(digits) <= 15:
        return None
    if raw.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    return f"+{digits}"


def find_phones(text: str) -> list[str]:
    """Phone numbers as they are written in the text, in order, de-duplicated.

    What a lookup wants is the normalized form; what a person reading the panel
    wants is the number the way the fleet typed it into the About text, which
    is the form they will recognise and dial. So both exist, and they agree on
    which runs of digits count as a phone number.
    """
    out: list[str] = []
    seen: set[str] = set()
    for chunk in PHONE_RE.findall(text or ""):
        chunk = chunk.strip()
        digits = re.sub(r"\D", "", chunk)
        if not 10 <= len(digits) <= 15:
            continue
        norm = normalize_phone(chunk)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(chunk)
    return out


def extract_phones(text: str) -> list[str]:
    """Phone numbers found in free text, normalized and de-duplicated."""
    return [normalize_phone(p) for p in find_phones(text)]
