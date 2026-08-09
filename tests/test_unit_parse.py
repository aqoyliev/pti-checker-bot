"""Title -> unit parsing, using real titles from the fleet.

The onboarding flow shows this value to an admin for confirmation, never writes
it directly, precisely because titles lie. These cases are the ones that
actually occur.
"""
from __future__ import annotations

import pytest

from utils.unit_parse import looks_retired, parse_unit


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
    # sub-leases: the FIRST number is the sublease, the second is the unit
    ("SUB 588197 // 212566 FARAH, MOHAMED ABDI", "212566"),
    ("SUB 429819 /// 728443 - EVARIS, PIERRE", "728443"),
    # no unit present at all
    ("GELIN, BIENNEL / EMMANUEL DEME", None),
    ("COBO, JORGE / BURGOS, JUAN", None),
    ("INACTIVE", None),
    ("", None),
    (None, None),
])
def test_parse_unit(title, expected):
    assert parse_unit(title) == expected


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
