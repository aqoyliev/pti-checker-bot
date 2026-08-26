"""Title -> unit parsing, using real titles from the fleet.

The onboarding flow shows this value to an admin for confirmation, never writes
it directly, precisely because titles lie. These cases are the ones that
actually occur.
"""
from __future__ import annotations

import pytest

from utils.unit_parse import (
    guess_unit,
    looks_retired,
    parse_unit,
    parse_unit_from_description,
    title_names_unit,
)


@pytest.mark.parametrize("title,expected", [
    # labelled forms
    ("UNIT# 1216 / MARTINEZ, JOSE / FERNANDEZ, MIGUEL", "1216"),
    ("UNIT 001 MORENO, IRAINUTH / VALLADARES, YOSVEL (MD)", "001"),
    ("Unit 1212 SAINVIL, JEAN EDDY / PIERRE, MARQUIS", "1212"),
    ("UNIT# 1151 / MARTINEZ ORTEGA, UBERLANDY", "1151"),
    ("TRUCK# 147085 VICTOR JEAN ROBERT ( COMPANY )", "147085"),
    # bare leading number
    ("1136 LORISTON, ALEX FRANCOIS // JOSEPH , ANTOINE", "1136"),
    ("0822 // FRANCOIS, DIKENS / HERNANDEZ, FRANCOIS", "0822"),
    ("1157 / MCCLAIN, DRANTON / SIMS, RANDY A", "1157"),
    # letters are part of the unit: glued in front, joined by a hyphen, or
    # trailing. All four shapes are live fleet numbers.
    ("F9121 BOYKIN, LEON / MARTINEZ HERNANDEZ, MARIO (ML)", "F9121"),
    ("ML2432 DEANS, DAVID", "ML2432"),
    ("T-120 QUINTERO, JOHN / LOUISSAINT, JEAN R (ML)", "T-120"),
    ("1002FT SMITH, JOHN", "1002FT"),
    # ... but never across a space, or a surname becomes part of the number
    ("1136 LO", "1136"),
    # ... but a unit buried after other words is still out of reach -- the
    # parser only ever looks at the very start of the title.
    ("TEAM \U0001F981 2336 IBRAAHIIMI, LIBAN / ABDIRAHMAN AWIL", None),
    # sub-leases: the FIRST number is the sublease, the second is the unit
    ("SUB 588197 // 212566 FARAH, MOHAMED ABDI", "212566"),
    ("SUB 429819 /// 728443 - EVARIS, PIERRE", "728443"),
    # ... including when "unit" sits between the two, which would otherwise let
    # the labelled rule claim the sublease number
    ("SUB-Unit# 543659 - 488090 - BRITO, DANYLLO / MEDEIROS, RONIVALDO", "488090"),
    # no unit present at all
    ("GELIN, BIENNEL / EMMANUEL DEME", None),
    ("COBO, JORGE / BURGOS, JUAN", None),
    ("INACTIVE", None),
    ("", None),
    (None, None),
])
def test_parse_unit(title, expected):
    assert parse_unit(title) == expected


@pytest.mark.parametrize("unit,title,expected", [
    # the 2026-08-26 regression: the number was on the title all along
    ("T-120", "T-120 QUINTERO, JOHN / LOUISSAINT, JEAN R (ML)", True),
    ("1225", "1225 / MAGAN", True),
    ("1225", "UNIT# 1225 MAGAN", True),
    ("1225", "MAGAN, MOHAMED", False),
    # whole tokens only, both edges
    ("120", "21203 MAGAN", False),
    ("1002", "MAGAN 1002FT", False),
    ("1002FT", "1002FT MAGAN", True),
    # case is not a reason to retire a truck
    ("t-120", "T-120 QUINTERO", True),
    ("", "1225 MAGAN", False),
    ("1225", None, False),
])
def test_title_names_unit(unit, title, expected):
    assert title_names_unit(title, unit) is expected


def test_labelled_unit_wins_over_a_leading_number():
    # "1275 - NEGRON..." is stored as unit 2003; a bare leading number is only a
    # guess, which is why an admin confirms it. But an explicit UNIT label must
    # always beat an incidental leading number.
    assert parse_unit("1275 - UNIT 2003 NEGRON, ALBERTO") == "2003"


@pytest.mark.parametrize("title", [
    "INACTIVE / ORTGEA NOELBIS / MONTES DE OCA",
    "Inactive // JOLIBOIS, FRANCKLIN",
    "INACTIVE GROUP CHAT // SAMUEL, ELROBY",
    "this group has been moved. Please use another one",
])
def test_looks_retired(title):
    assert looks_retired(title) is True


@pytest.mark.parametrize("title", [
    "UNIT# 1216 / MARTINEZ, JOSE",
    "1136 LORISTON, ALEX",
    "",
    None,
])
def test_not_retired(title):
    assert looks_retired(title) is False


# --- description ---------------------------------------------------------
# A group's About text is free prose, so only the *labelled* forms count there.
# The bare-leading-number rule that works on titles would read a phone number,
# a year or a DOT number as a unit.

@pytest.mark.parametrize("description, expected", [
    ("Unit 1216 — Smith. Call dispatch at 555-0134.", "1216"),
    ("TRUCK# 147085", "147085"),
    ("Driver group. UNIT: 2003", "2003"),
    ("SUB 588197 // 212566 FARAH", "212566"),
    # Must NOT be read as a unit:
    ("Call 5550134 for dispatch", None),
    ("Established 2019", None),
    ("1216 Riverside Ave, Newark NJ", None),
    ("DOT 3947281", None),
    ("", None),
    (None, None),
])
def test_parse_unit_from_description(description, expected):
    assert parse_unit_from_description(description) == expected


def test_guess_unit_prefers_the_title():
    # The title is the field the fleet keeps current; About text goes stale.
    assert guess_unit("UNIT 1216 SMITH", "Unit 9999 old truck") == ("1216", "title")


def test_guess_unit_falls_back_to_the_description():
    assert guess_unit("Smith / Jones", "Unit 1216 assigned") == ("1216", "description")


def test_guess_unit_reports_no_source_when_nothing_matches():
    assert guess_unit("Smith / Jones", "no numbers here") == (None, "")


def test_guess_unit_ignores_unlabelled_numbers_in_the_description():
    # A title with no unit plus a description holding only a phone number must
    # produce no guess at all, rather than a confidently wrong one.
    assert guess_unit("MARCANO", "reach us on 5550134") == (None, "")
