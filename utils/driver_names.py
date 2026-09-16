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

from utils.phones import find_phones

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


def tidy_name(raw: str | None) -> str:
    """The one stored shape of a driver name: 'SAINTIL,  FEDJ' -> 'Saintil Fedj'.

    Every name that reaches `group_drivers` goes through this (utils/db applies
    it on each write), whoever typed it: the About text, the title, an admin in
    the panel or `/adddriver`. Before that, an automatic setup stored
    "Vazquez Lizbeth" and an admin fixing the group next door stored
    "SAINTIL, FEDJ", and the fleet report printed both styles down one column.

    Commas and runs of whitespace go -- the fleet writes SURNAME, GIVEN, and the
    comma is punctuation, not part of the name. A name typed in one case (all
    capitals, or all lower case) is title-cased. A name with any mixed casing is
    left exactly as it is: that is somebody's deliberate spelling, and .title()
    would turn McDonald into Mcdonald.
    """
    name = " ".join((raw or "").replace(",", " ").split())
    if name.isupper() or name.islower():
        return name.title()
    return name


def _clean_name(raw: str) -> str:
    """'ZAMA, EMILE ' -> 'Zama Emile'. Empty when it isn't a name at all."""
    name = tidy_name(raw)
    if not name or len(name) > MAX_NAME_CHARS:
        return ""
    # A digit means the line was "Driver: 718-864-1154" or similar -- a label,
    # not a name. Storing it would be worse than falling back to Telegram.
    # (A name an admin types is not held to this: "Lovensky 509" is how that
    # driver is known, and tidy_name keeps it.)
    if any(ch.isdigit() for ch in name) or not _HAS_LETTER.search(name):
        return ""
    return name


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


# A leading `Driver 1 -` / `Name:` / `Numbers` label, and the punctuation left
# behind once a number is struck out of the line. The list covers what the
# *number* half of a line is called too, because "Phone# 718-864-1154" with the
# number cut out is the word "Phone" — which is a perfectly good name unless
# something says otherwise.
_LABEL = re.compile(
    r"^[^\S\n]*(?:driver|name|phone|number|cell|mobile|tel|contact)s?"
    r"[^\S\n]*\d*[^\S\n]*[-:#–—]*", re.I)
_EDGE_PUNCT = "-–—:#/.,;· \t"


def _name_chunks(line: str) -> list[str]:
    """The names a single line holds, `/`-separated, label stripped.

    Empty when any chunk isn't a name, so a line is all names or none of them.
    "718-864-1154 / 561-667-4276" cleans to nothing and "Home in Baltimore
    AUGUST18-20" carries digits, which `_clean_name` already refuses.
    """
    parts = [p for p in _LABEL.sub("", line, count=1).split("/")]
    names = [_clean_name(p.strip(_EDGE_PUNCT)) for p in parts if p.strip(_EDGE_PUNCT)]
    return names if names and all(names) else []


def parse_driver_contacts(text: str) -> list[tuple[str, str | None]]:
    """(name, phone-as-written) pairs from the About text, best evidence first.

    Three layouts, tried in order of how much guessing the pairing costs. The
    number is returned **as the fleet typed it** — that is the form an admin
    recognises and dials; the normalized form is what a lookup wants, and
    `utils/phones` has both.

    1. **Name and number on the same line** — ``Driver 1 - ZAMA, EMILE -
       718-864-1154``. The fleet wrote them together, so the pairing is the
       fleet's and not a reconstruction: it survives a line being added,
       reordered or left blank. Needs exactly one number on the line.
    2. **A names line directly above a numbers line**, both `/`-separated, with
       the same count on each. This is the layout with no label at all
       (``MATTHEWS, CHRISTOPHER / MCLAUGHLIN, ARTESIA`` then the two numbers),
       which `parse_driver_names` refuses on its own — a bare line of prose is
       not a name list. Sitting immediately above a matching count of phone
       numbers is the evidence it otherwise lacks.
    3. **Whole-document positional** — labelled ``Name:`` and number lines,
       paired by position, which is what `utils/auto_onboard` already does for
       the same text. Only when the counts match.

    Falling through all three leaves the numbers unattributed and the caller
    shows them as the *group's* numbers rather than guessing whose they are.
    Putting the wrong number on a driver reads as authoritative, exactly like a
    wrong name does, and here it would have someone call the wrong person.

    A name with no number still comes back, paired with None: the name is the
    useful half, and half an answer beats none.
    """
    lines = (text or "").splitlines()

    # Layout 1.
    inline: list[tuple[str, str | None]] = []
    for line in lines:
        found = find_phones(line)
        if len(found) != 1:
            continue
        names = _name_chunks(line.replace(found[0], " "))
        if len(names) == 1:
            inline.append((names[0], found[0]))
    if inline:
        return inline

    # Layout 2.
    previous = ""
    for line in lines:
        found = find_phones(line)
        if not found:
            if line.strip():
                previous = line
            continue
        names = _name_chunks(previous)
        if len(names) == len(found):
            return list(zip(names, found))
        previous = ""

    # Layout 3.
    names = parse_driver_names(text)
    phones = find_phones(text)
    if names and len(names) == len(phones):
        return list(zip(names, phones))
    return [(n, None) for n in names]


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
