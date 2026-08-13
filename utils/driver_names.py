"""The fleet's name for a driver: reading it, and pairing it to a user_id.

Everything here is keyed on `user_id`, but a `user_id` is not a name an admin
can look up. A Telegram profile says "Emile" with a plane emoji, or nothing but
a handle, while the fleet's driver list says ZAMA, EMILE -- so the name worth
storing is the one the fleet writes into the group's About text:

    Name: ZAMA, EMILE / FLEURMOND, JACQUES
    Phone# 718-864-1154 / 561-667-4276
    Truck# 1239

Onboarding pairs those names with the numbers *by position*, because it is
resolving the numbers anyway. Nothing else can: a group configured before this
existed has user_ids and Telegram names in the database and no phone lookup left
to run cheaply -- contact import is the most rate-limited thing a user account
does, and doing it fleet-wide to fix a display name is not a trade worth making.

So `match_names_to_drivers` pairs on the names themselves, and only where the
pairing is *proven*: a shared word between the fleet name and the stored one,
matching exactly one driver, with no driver claimed twice. A name that can't be
placed is returned unplaced rather than guessed at -- putting the wrong name on
a user_id is worse than leaving a Telegram handle in place, because it reads as
authoritative.

Pure: no DB, no Telegram.
"""
from __future__ import annotations

import re

# A separator (`Name:` / `Name#`) is required, for the same reason descriptions
# are parsed more strictly than titles: About text is free prose, and a bare
# "name" would match a sentence about one.
_NAME_LINE = re.compile(r"^[^\S\n]*(?:driver|name)s?[^\S\n]*[:#][^\S\n]*(.+)$",
                        re.I | re.M)
_HAS_LETTER = re.compile(r"[^\W\d_]")
# Words worth matching on: three letters or more, so initials and "Jr" can't
# pair two unrelated people.
_WORD = re.compile(r"[^\W\d_]{3,}")
MAX_NAME_CHARS = 64


def _clean_name(raw: str) -> str:
    """'ZAMA, EMILE ' -> 'Zama Emile'. Empty when it isn't a name at all."""
    name = " ".join(raw.replace(",", " ").split())
    if not name or len(name) > MAX_NAME_CHARS:
        return ""
    # A digit means the line was "Driver: 718-864-1154" or similar -- a label,
    # not a name. Storing it would be worse than falling back to Telegram.
    if any(ch.isdigit() for ch in name) or not _HAS_LETTER.search(name):
        return ""
    # The fleet's lists SHOUT, but a properly-cased name is left alone:
    # .title() would turn McDonald into Mcdonald.
    return name.title() if name.isupper() else name


def parse_driver_names(text: str) -> list[str]:
    """Driver names from the About text, in the order they are written.

    Handles both layouts the fleet uses: both names on one line separated by
    '/', or one `Name:` line per driver.
    """
    out: list[str] = []
    for line in _NAME_LINE.findall(text or ""):
        for part in line.split("/"):
            name = _clean_name(part)
            if name and name not in out:
                out.append(name)
    return out


def _words(name: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(name or "")}


def match_names_to_drivers(names: list[str],
                           drivers: list[dict]) -> tuple[dict[int, str], list[str]]:
    """Pair fleet names to registered drivers by their words.

    Returns ({user_id: fleet name}, names that could not be placed). `drivers`
    is [{"user_id": int, "name": str}, ...] as stored.
    """
    if not names or not drivers:
        return {}, list(names)

    # One name and one driver needs no evidence: the About text belongs to this
    # group and there is only one person it can be about.
    if len(names) == 1 and len(drivers) == 1:
        return {drivers[0]["user_id"]: names[0]}, []

    hits: dict[str, list[int]] = {}
    for name in names:
        words = _words(name)
        hits[name] = [d["user_id"] for d in drivers
                      if words & _words(d.get("name") or "")]

    # Two drivers called MOHAMED both match "MOHAMED, ALI"; so does the reverse.
    # Either way the pair is unproven, so neither name is placed.
    taken: dict[int, int] = {}
    for found in hits.values():
        if len(found) == 1:
            taken[found[0]] = taken.get(found[0], 0) + 1

    placed: dict[int, str] = {}
    unplaced: list[str] = []
    for name, found in hits.items():
        if len(found) == 1 and taken[found[0]] == 1:
            placed[found[0]] = name
        else:
            unplaced.append(name)
    return placed, unplaced
