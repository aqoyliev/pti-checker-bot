"""The automatic title sweep: what it retires, and when it runs.

Pure: no DB, no Telegram. The sweep writes without an admin confirming, so the
guards on *what it declines to touch* are the whole test surface.
"""
from datetime import date, datetime

from handlers.admin.units import (
    apply_renames,
    title_deactivations,
    title_sweep_due,
    title_unit_changes,
)


def _g(gid: int, unit, title, active=True) -> dict:
    return {"group_id": gid, "unit_number": unit, "title": title, "is_active": active}


def _dead(groups: list[dict], units: list[str]) -> list[int]:
    return [c["group"]["group_id"] for c in title_deactivations(groups, units)]


# ---------- what gets retired ----------

def test_title_with_no_unit_is_deactivated():
    assert _dead([_g(1, "1225", "MAGAN, MOHAMED")], ["1225"]) == [1]


def test_retired_marker_is_deactivated_even_with_a_number():
    # "INACTIVE - 1225 MAGAN" parses fine; the marker still means the truck left.
    assert _dead([_g(1, "1225", "INACTIVE - 1225 MAGAN")], ["1225"]) == [1]


def test_title_naming_an_unlisted_unit_is_deactivated():
    # Filed as 1225, title claims 9999, and the fleet says 9999 isn't running.
    assert _dead([_g(1, "1225", "UNIT 9999 / MAGAN")], ["1225", "1330"]) == [1]


def test_the_reason_names_the_rule_that_fired():
    reasons = [c["reason"] for c in title_deactivations(
        [_g(1, "1225", "MAGAN"), _g(2, "1400", "UNIT 9999 / B")], ["1225", "1400"])]
    assert reasons == ["title no longer names a unit",
                       "title names 9999, not on the active list"]


def test_title_still_naming_a_unit_is_left_alone():
    assert _dead([_g(1, "1225", "1225 / MAGAN")], ["1225"]) == []


def test_title_agreeing_with_a_delisted_stored_unit_is_left_alone():
    # Title and database agree; retiring a truck for falling off the list is the
    # weekly /units sweep's call, where an admin confirms it.
    assert _dead([_g(1, "1225", "1225 / MAGAN")], ["1330"]) == []


# ---------- what it refuses to touch ----------

def test_an_empty_units_list_disables_the_unlisted_rule():
    # Otherwise the first sweep on a fresh database retires the whole fleet.
    assert _dead([_g(1, "1225", "UNIT 9999 / MAGAN")], []) == []


def test_an_empty_units_list_still_retires_a_title_with_no_unit():
    # That rule reads the title alone and never consults the list.
    assert _dead([_g(1, "1225", "INACTIVE - MAGAN")], []) == [1]


def test_unconfigured_group_is_never_retired():
    # No stored unit means no truck to retire, and its title not parsing is the
    # very question onboarding is waiting to ask an admin.
    assert _dead([_g(1, None, "Dispatch chat")], ["1225"]) == []


def test_unreadable_title_is_not_evidence():
    # _refresh_titles drops groups it couldn't fetch; a blank must never read as
    # "the title lost its unit" and retire a group for being unreachable.
    assert _dead([_g(1, "1225", None)], ["1225"]) == []


def test_already_inactive_group_is_skipped():
    assert _dead([_g(1, "1225", "MAGAN", active=False)], ["1225"]) == []


# ---------- re-file first, then retire ----------

def test_a_renamed_group_is_not_retired_for_losing_its_old_number():
    groups = [_g(1, "1225", "UNIT 1330 / MAGAN")]
    renames = title_unit_changes(groups, ["1330"])
    assert _dead(apply_renames(groups, renames), ["1330"]) == []


def test_rename_and_retire_can_both_happen_in_one_sweep():
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1400", "INACTIVE - B")]
    units = ["1330", "1400"]
    renames = title_unit_changes(groups, units)
    after = apply_renames(groups, renames)
    assert [(r["old"], r["new"]) for r in renames] == [("1225", "1330")]
    assert _dead(after, units) == [2]


def test_a_collision_leaves_both_groups_alone():
    # Two titles claiming 1330: neither is re-filed (guessing which is right is
    # how two groups end up under one unit), and neither is retired either --
    # 1330 is on the list, so the unlisted rule doesn't fire. An unresolved
    # collision must not turn into a deactivation through the back door.
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1226", "UNIT 1330 / B")]
    units = ["1330"]
    renames = title_unit_changes(groups, units)
    assert renames == []
    assert _dead(apply_renames(groups, renames), units) == []


# ---------- the schedule ----------

def _at(y, m, d) -> datetime:
    return datetime(y, m, d, 7, 0)


def test_runs_on_monday_wednesday_friday():
    # 2026-08-10 is a Monday.
    assert title_sweep_due(_at(2026, 8, 10), None)
    assert title_sweep_due(_at(2026, 8, 12), None)
    assert title_sweep_due(_at(2026, 8, 14), None)


def test_does_not_run_on_other_days():
    assert not title_sweep_due(_at(2026, 8, 11), None)
    assert not title_sweep_due(_at(2026, 8, 13), None)
    assert not title_sweep_due(_at(2026, 8, 15), None)
    assert not title_sweep_due(_at(2026, 8, 16), None)


def test_does_not_run_twice_in_one_day():
    # The six-hourly tick hits every sweep day four times over.
    assert not title_sweep_due(_at(2026, 8, 12), date(2026, 8, 12))


def test_a_previous_sweep_does_not_block_the_next_day():
    assert title_sweep_due(_at(2026, 8, 12), date(2026, 8, 10))
