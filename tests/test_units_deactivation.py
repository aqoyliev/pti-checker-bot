"""Which groups the weekly /units list retires.

Pure: no DB. `groups_to_deactivate` decides the blast radius of a fleet-wide
write driven by one pasted message, so its edges are worth pinning down.
"""
from handlers.admin.units import groups_to_deactivate


def _g(gid: int, unit, active=True, title=None) -> dict:
    return {"group_id": gid, "unit_number": unit, "is_active": active,
            "title": title or f"Truck {unit}"}


def test_group_off_the_list_is_deactivated():
    groups = [_g(1, "1332"), _g(2, "9999")]
    out = groups_to_deactivate(groups, ["1332"])
    assert [g["group_id"] for g in out] == [2]


def test_group_on_the_list_is_kept():
    assert groups_to_deactivate([_g(1, "1332")], ["1332", "1303"]) == []


def test_group_without_a_unit_is_left_alone():
    # Not yet onboarded — the list is about trucks, and "no unit" is not
    # "retired truck". Sweeping these would retire every un-configured group.
    groups = [_g(1, None), _g(2, ""), _g(3, "   ")]
    assert groups_to_deactivate(groups, ["1332"]) == []


def test_already_inactive_group_is_not_reselected():
    groups = [_g(1, "9999", active=False)]
    assert groups_to_deactivate(groups, ["1332"]) == []


def test_units_are_compared_normalized():
    # Group units were typed or imported with angle brackets/whitespace; the
    # pasted list is bare digits. "<1332 >" and "1332" are the same truck.
    assert groups_to_deactivate([_g(1, "<1332 >")], ["1332"]) == []


def test_leading_zeros_are_significant():
    # "0822" and "822" are different trucks — normalize_unit strips junk, not zeros.
    out = groups_to_deactivate([_g(1, "0822")], ["822"])
    assert [g["group_id"] for g in out] == [1]


def test_empty_list_retires_every_unit_bearing_group():
    # Nothing is written on this path without an explicit confirmation, which is
    # exactly why the preview exists: a truncated paste looks like this.
    groups = [_g(1, "1332"), _g(2, "1303"), _g(3, None)]
    out = groups_to_deactivate(groups, [])
    assert [g["group_id"] for g in out] == [1, 2]


def test_missing_is_active_key_defaults_to_active():
    # get_all_groups rows always carry is_active, but a caller building a dict
    # by hand shouldn't silently skip the group.
    out = groups_to_deactivate([{"group_id": 7, "unit_number": "9999"}], ["1332"])
    assert [g["group_id"] for g in out] == [7]
