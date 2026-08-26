"""Re-filing a group when its title starts naming a different unit.

Pure: no DB. This decides whether a truck's group gets moved to another unit
number automatically. Since 2026-08-26 the title is taken at its word -- the
stored active-units list is no longer consulted -- so what is left to test is
that the *integrity* guards survived that.
"""
from handlers.admin.units import (
    apply_renames,
    groups_to_deactivate,
    title_unit_changes,
)


def _g(gid: int, unit, title, active=True) -> dict:
    return {"group_id": gid, "unit_number": unit, "title": title, "is_active": active}


# ---------- the happy path ----------

def test_a_title_naming_another_unit_is_re_filed():
    groups = [_g(1, "1225", "UNIT 1330 / MAGAN, MOHAMED")]
    out = title_unit_changes(groups)
    assert [(r["old"], r["new"]) for r in out] == [("1225", "1330")]


def test_unchanged_title_is_not_a_rename():
    assert title_unit_changes([_g(1, "1225", "1225 / MAGAN")]) == []


# ---------- the guards ----------

def test_the_new_unit_does_not_have_to_be_on_the_stored_list():
    """Dropped 2026-08-26: the title naming a new unit IS the truck changing.

    The list is pasted by hand and goes stale between pastes, so requiring it
    meant a truck that had just arrived -- the case a rename exists for -- was
    skipped in silence.
    """
    out = title_unit_changes([_g(1, "1225", "UNIT 9999 / MAGAN")])
    assert [(r["old"], r["new"]) for r in out] == [("1225", "9999")]


def test_retired_looking_titles_are_left_alone():
    # About to be deactivated anyway, and their titles are the least reliable.
    groups = [_g(1, "1225", "INACTIVE - UNIT 1330 MAGAN")]
    assert title_unit_changes(groups) == []


def test_unconfigured_group_is_not_re_filed():
    # No stored unit — that's onboarding's decision, and it asks a human.
    assert title_unit_changes([_g(1, None, "UNIT 1330 / MAGAN")]) == []


def test_inactive_group_is_not_re_filed():
    assert title_unit_changes([_g(1, "1225", "UNIT 1330", active=False)]) == []


def test_two_groups_claiming_one_unit_are_both_skipped():
    # Guessing which is right is exactly how two groups end up under one unit.
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1226", "UNIT 1330 / B")]
    assert title_unit_changes(groups) == []


def test_unit_already_held_by_another_active_group_is_skipped():
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1330", "1330 / B")]
    assert title_unit_changes(groups) == []


def test_titleless_group_is_skipped():
    assert title_unit_changes([_g(1, "1225", None)]) == []


def test_units_compare_normalized():
    # Stored "<1330 >" is unit 1330, so the title names no change at all.
    assert title_unit_changes([_g(1, "<1330 >", "UNIT 1330 / A")]) == []


# ---------- ordering: rename before deactivate ----------

def test_renamed_group_is_not_deactivated_for_its_old_unit():
    # The whole reason renames run first. Truck moved 1225 -> 1330; 1330 is on
    # the weekly list and 1225 is not. Judged on the old number -- which the
    # /units sweep below still does consult the list for -- it would be retired.
    groups = [_g(1, "1225", "UNIT 1330 / MAGAN")]
    units = ["1330"]
    renames = title_unit_changes(groups)
    after = apply_renames(groups, renames)
    assert groups_to_deactivate(after, units) == []


def test_a_genuinely_retired_group_still_gets_deactivated():
    groups = [_g(1, "1225", "1225 / MAGAN")]
    units = ["1330"]
    renames = title_unit_changes(groups)
    after = apply_renames(groups, renames)
    assert [g["group_id"] for g in groups_to_deactivate(after, units)] == [1]


def test_apply_renames_leaves_other_groups_untouched():
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1400", "1400 / B")]
    out = apply_renames(groups, title_unit_changes(groups))
    assert [(g["group_id"], g["unit_number"]) for g in out] == [(1, "1330"), (2, "1400")]
