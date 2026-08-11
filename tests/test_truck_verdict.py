"""What a PTI's truck reading means — the plate-guarded change rule.

Pure: no DB, no Telegram. `truck_verdict` decides whether a group's registered
truck gets overwritten, so a wrong answer here silently misattributes every
later inspection in that group.
"""
import pytest

from handlers.groups.pti import truck_verdict


# stored_unit, stored_plate, seen_unit, seen_plate -> action
CASES = [
    # A differing unit with the SAME plate is the same truck filmed badly. The
    # plate is the more legible marking, so it wins and nothing is stored.
    (("1332", "ABC123", "1882", "ABC123"), "misread"),
    # Differing unit AND differing plate — a real swap.
    (("1332", "ABC123", "1882", "XYZ789"), "change"),
    # Differing unit, no plate visible in the video — treated as a real swap, so
    # a genuine change filmed without a plate shot still registers.
    (("1332", "ABC123", "1882", None), "change"),
    (("1332", None, "1882", None), "change"),
    # Nothing registered yet to compare against.
    ((None, None, "1882", "XYZ789"), "plate"),
    # Same unit, new plate reading — adopt the plate.
    (("1332", "ABC123", "1332", "XYZ789"), "plate"),
    (("1332", None, "1332", "ABC123"), "plate"),
    # Nothing new at all.
    (("1332", "ABC123", "1332", "ABC123"), "none"),
    (("1332", "ABC123", None, None), "none"),
    (("1332", "ABC123", "1332", None), "none"),
]


@pytest.mark.parametrize("args,expected", CASES)
def test_truck_verdict(args, expected):
    assert truck_verdict(*args)[0] == expected


def test_change_returns_the_new_unit_and_plate():
    action, unit, plate = truck_verdict("1332", "ABC123", "1882", "XYZ789")
    assert (action, unit, plate) == ("change", "1882", "XYZ789")


def test_misread_stores_nothing():
    # The caller writes only what it is handed back; a misread must hand back
    # neither the unit nor the plate.
    assert truck_verdict("1332", "ABC123", "1882", "ABC123") == ("misread", None, None)


def test_plate_action_carries_only_the_plate():
    action, unit, plate = truck_verdict("1332", "ABC123", "1332", "XYZ789")
    assert (action, unit, plate) == ("plate", None, "XYZ789")


def test_whitespace_only_readings_are_not_values():
    # Gemini occasionally returns "  " rather than omitting the field; that is
    # absence, not a new plate, and must not overwrite a good stored one.
    assert truck_verdict("1332", "ABC123", "  ", "  ")[0] == "none"


def test_plate_match_is_exact():
    # No normalisation is applied to plates — a differing plate string means a
    # differing plate, so this is a change rather than a misread.
    assert truck_verdict("1332", "ABC 123", "1882", "ABC123")[0] == "change"
