"""Re-filing a group when its title starts naming a different unit.

Pure: no DB. This decides whether a truck's group gets moved to another unit
number automatically, so the guards against a bad parse are the point.
"""
from handlers.admin.units import (
    apply_renames,
    groups_to_deactivate,
    title_unit_changes,
)


def _g(gid: int, unit, title, active=True) -> dict:
    return {"group_id": gid, "unit_number": unit, "title": title, "is_active": active}


# ---------- the happy path ----------

def test_title_naming_a_listed_unit_is_re_filed():
    groups = [_g(1, "1225", "UNIT 1330 / MAGAN, MOHAMED")]
    out = title_unit_changes(groups, ["1330", "1400"])
    assert [(r["old"], r["new"]) for r in out] == [("1225", "1330")]


def test_unchanged_title_is_not_a_rename():
    assert title_unit_changes([_g(1, "1225", "1225 / MAGAN")], ["1225"]) == []


# ---------- the guards ----------

def test_new_unit_must_be_on_the_incoming_list():
    # The corroboration that separates a real swap from a parsed phone number,
    # year or typo. Without it this is a bare 79.5%-accurate regex write.
    assert title_unit_changes([_g(1, "1225", "UNIT 9999 / MAGAN")], ["1225", "1330"]) == []


def test_retired_looking_titles_are_left_alone():
    # About to be deactivated anyway, and their titles are the least reliable.
    groups = [_g(1, "1225", "INACTIVE - UNIT 1330 MAGAN")]
    assert title_unit_changes(groups, ["1330"]) == []


def test_unconfigured_group_is_not_re_filed():
    # No stored unit — that's onboarding's decision, and it asks a human.
    assert title_unit_changes([_g(1, None, "UNIT 1330 / MAGAN")], ["1330"]) == []


def test_inactive_group_is_not_re_filed():
    assert title_unit_changes([_g(1, "1225", "UNIT 1330", active=False)], ["1330"]) == []


def test_two_groups_claiming_one_unit_are_both_skipped():
    # Guessing which is right is exactly how two groups end up under one unit.
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1226", "UNIT 1330 / B")]
    assert title_unit_changes(groups, ["1330"]) == []


def test_unit_already_held_by_another_active_group_is_skipped():
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1330", "1330 / B")]
    assert title_unit_changes(groups, ["1330", "1225"]) == []


def test_titleless_group_is_skipped():
    assert title_unit_changes([_g(1, "1225", None)], ["1330"]) == []


def test_units_compare_normalized():
    assert title_unit_changes([_g(1, "<1330 >", "UNIT 1330 / A")], ["1330"]) == []


# ---------- ordering: rename before deactivate ----------

def test_renamed_group_is_not_deactivated_for_its_old_unit():
    # The whole reason renames run first. Truck moved 1225 -> 1330; 1330 is on
    # the list and 1225 is not. Judged on the old number it would be retired.
    groups = [_g(1, "1225", "UNIT 1330 / MAGAN")]
    units = ["1330"]
    renames = title_unit_changes(groups, units)
    after = apply_renames(groups, renames)
    assert groups_to_deactivate(after, units) == []


def test_a_genuinely_retired_group_still_gets_deactivated():
    groups = [_g(1, "1225", "1225 / MAGAN")]
    units = ["1330"]
    renames = title_unit_changes(groups, units)
    after = apply_renames(groups, renames)
    assert [g["group_id"] for g in groups_to_deactivate(after, units)] == [1]


def test_apply_renames_leaves_other_groups_untouched():
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1400", "1400 / B")]
    out = apply_renames(groups, title_unit_changes(groups, ["1330", "1400"]))
    assert [(g["group_id"], g["unit_number"]) for g in out] == [(1, "1330"), (2, "1400")]
