"""The fleet's own roster paste: unit number, trailer number, drivers.

The weekly `/units` list is a bare list of unit numbers. The roster the office
keeps is wider -- one line per truck, in three whitespace-separated columns:

    4024  ST510278  AHMED, IBRAHIM HASHI / ABDULLAHI, ABDI
    396497  BOX TRUCK  HORTON, ROBERT (ML) SOLO
    2432  671316 LOT  DEANS, DAVID (ML)

Columns are separated by **two or more** spaces, which is what makes this
parseable at all: a trailer is sometimes two words ("BOX TRUCK", "671316 LOT")
and a driver name always contains single-spaced words, so a naive
split-on-whitespace would put half a trailer in the driver column and the
"first digit run" rule used elsewhere would read the trailer as a second unit.

Only the first two columns are read. The driver names are deliberately ignored
here -- a name is worth storing against a `user_id`, and this file has no user
ids in it (see utils/driver_names.py for where names do get stored).

Nothing is guessed. A line that doesn't have the three columns, or whose second
column looks like a name rather than a trailer, is returned as a *problem* for
a human to look at rather than being dropped quietly: a roster is the list that
decides which trucks the bot will accept, so a silently skipped line is a truck
that can never be onboarded.

Pure: no DB, no Telegram.
"""
from __future__ import annotations

import re

_COLUMNS = re.compile(r"\s{2,}")
# Units are not always digits ("1002FT", "F9121"), and leading zeros are real
# ("0942"), so a unit stays a string and is only checked for *shape*.
_UNIT = re.compile(r"^[A-Z0-9][A-Z0-9\-]{1,11}$", re.IGNORECASE)
MAX_TRAILER_CHARS = 32


def parse_roster(text: str) -> tuple[list[dict], list[dict]]:
    """``(rows, problems)`` from a pasted roster.

    ``rows`` is ``[{"unit": str, "trailer": str}, ...]`` in the order given;
    ``problems`` is ``[{"line": str, "reason": str}, ...]``.

    A repeated unit is a problem rather than a last-one-wins overwrite: the two
    lines disagree about which trailer the truck is pulling, and picking one is
    the sort of quiet guess that misattributes an inspection later.
    """
    rows: list[dict] = []
    problems: list[dict] = []
    seen: dict[str, str] = {}

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue

        columns = _COLUMNS.split(line)
        if len(columns) < 3:
            problems.append({"line": line, "reason": "expected 3 columns "
                                                     "(unit, trailer, drivers)"})
            continue

        unit, trailer = columns[0].strip(), columns[1].strip()
        if not _UNIT.match(unit):
            problems.append({"line": line, "reason": f"{unit!r} is not a unit number"})
            continue
        # A comma or a slash in the trailer column means the columns slid: the
        # driver names landed where the trailer should be.
        if not trailer or "," in trailer or "/" in trailer:
            problems.append({"line": line, "reason": "trailer column looks like a name"})
            continue
        if len(trailer) > MAX_TRAILER_CHARS:
            problems.append({"line": line, "reason": "trailer column too long"})
            continue
        if unit in seen:
            problems.append({"line": line,
                             "reason": f"unit {unit} already listed with trailer "
                                       f"{seen[unit]}"})
            continue

        seen[unit] = trailer
        rows.append({"unit": unit, "trailer": trailer})

    return rows, problems
