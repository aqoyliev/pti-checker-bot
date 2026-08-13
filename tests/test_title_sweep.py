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


# ---------- what gets retired ----------

def test_title_with_no_unit_is_deactivated():
    assert [g["group_id"] for g in title_deactivations([_g(1, "1225", "MAGAN, MOHAMED")])] == [1]


def test_retired_marker_is_deactivated_even_with_a_number():
    # "INACTIVE - 1225 MAGAN" parses fine; the marker still means the truck left.
    groups = [_g(1, "1225", "INACTIVE - 1225 MAGAN")]
    assert [g["group_id"] for g in title_deactivations(groups)] == [1]


def test_title_still_naming_a_unit_is_left_alone():
    assert title_deactivations([_g(1, "1225", "1225 / MAGAN")]) == []


def test_title_naming_an_unlisted_unit_is_left_alone():
    # Neither re-filed (not on the active list) nor retired (it names a unit).
    groups = [_g(1, "1225", "UNIT 9999 / MAGAN")]
    assert title_unit_changes(groups, ["1225", "1330"]) == []
    assert title_deactivations(groups) == []


# ---------- what it refuses to touch ----------

def test_unconfigured_group_is_never_retired():
    # No stored unit means no truck to retire, and its title not parsing is the
    # very question onboarding is waiting to ask an admin.
    assert title_deactivations([_g(1, None, "Dispatch chat")]) == []


def test_unreadable_title_is_not_evidence():
    # _refresh_titles drops groups it couldn't fetch; a blank must never read as
    # "the title lost its unit" and retire a group for being unreachable.
    assert title_deactivations([_g(1, "1225", None)]) == []


def test_already_inactive_group_is_skipped():
    assert title_deactivations([_g(1, "1225", "MAGAN", active=False)]) == []


# ---------- re-file first, then retire ----------

def test_a_renamed_group_is_not_retired_for_losing_its_old_number():
    groups = [_g(1, "1225", "UNIT 1330 / MAGAN")]
    renames = title_unit_changes(groups, ["1330"])
    assert title_deactivations(apply_renames(groups, renames)) == []


def test_rename_and_retire_can_both_happen_in_one_sweep():
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1400", "INACTIVE - B")]
    renames = title_unit_changes(groups, ["1330", "1400"])
    after = apply_renames(groups, renames)
    assert [(r["old"], r["new"]) for r in renames] == [("1225", "1330")]
    assert [g["group_id"] for g in title_deactivations(after)] == [2]


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
