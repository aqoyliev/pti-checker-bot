"""Trucks with no chat: the other half of the weekly reconciliation.

Pure: no DB. groups_to_deactivate finds chats whose truck is gone; this finds
trucks whose chat is missing -- the units that can never produce an inspection,
and which nothing else in the bot would ever mention, because every other report
is driven off the groups it already knows about.
"""
from handlers.admin.units import (
    apply_renames,
    title_unit_changes,
    units_without_groups,
)


def _g(gid: int, unit, title="chat", active=True) -> dict:
    return {"group_id": gid, "unit_number": unit, "title": title, "is_active": active}


def _missing(groups, units) -> list[str]:
    return [m["unit"] for m in units_without_groups(groups, units)]


def test_a_listed_unit_with_no_group_is_reported():
    assert _missing([_g(1, "1225")], ["1225", "1330"]) == ["1330"]


def test_every_unit_having_a_group_reports_nothing():
    assert _missing([_g(1, "1225"), _g(2, "1330")], ["1225", "1330"]) == []


def test_units_keep_the_order_they_were_pasted_in():
    assert _missing([], ["1330", "1225", "0822"]) == ["1330", "1225", "0822"]


def test_a_repeated_unit_is_reported_once():
    assert _missing([], ["1225", "1225"]) == ["1225"]


def test_units_compare_normalized():
    # Group units were typed or imported ("<1304 >"); the list is bare digits.
    assert _missing([_g(1, "<1225 >")], ["1225"]) == []


def test_a_deactivated_group_still_counts_as_no_active_group():
    # It cannot post an inspection, so the truck is unwatched either way.
    out = units_without_groups([_g(1, "1225", active=False)], ["1225"])

    assert [m["unit"] for m in out] == ["1225"]
    # ...but the fix is a reactivation, not a new chat, so the group is attached.
    assert out[0]["inactive"]["group_id"] == 1


def test_a_missing_unit_has_no_group_attached():
    assert units_without_groups([], ["1225"]) == [{"unit": "1225", "inactive": None}]


def test_an_active_group_outranks_a_deactivated_one_on_the_same_unit():
    # A truck re-homed into a new chat leaves the old one deactivated; the unit
    # is covered, and reporting it would send the admin chasing a live group.
    groups = [_g(1, "1225", active=False), _g(2, "1225")]
    assert _missing(groups, ["1225"]) == []


def test_unconfigured_groups_do_not_cover_a_unit():
    # A group with no stored unit is exactly the case where the truck has a chat
    # but no way to attribute anything to it -- that is still a gap.
    assert _missing([_g(1, None)], ["1225"]) == ["1225"]


def test_a_renamed_group_covers_its_new_unit_not_its_old_one():
    # Run on post-rename rows, or a truck that just moved reads as missing under
    # the number it no longer has.
    groups = [_g(1, "1225", "UNIT 1330 / MAGAN")]
    units = ["1330"]
    moved = apply_renames(groups, title_unit_changes(groups))

    assert _missing(moved, units) == []
    assert _missing(groups, units) == ["1330"]
