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


def _dead(groups: list[dict]) -> list[int]:
    return [c["group"]["group_id"] for c in title_deactivations(groups)]


# ---------- what gets retired ----------

def test_title_with_no_unit_is_deactivated():
    assert _dead([_g(1, "1225", "MAGAN, MOHAMED")]) == [1]


def test_retired_marker_is_deactivated_even_with_a_number():
    # "INACTIVE - 1225 MAGAN" parses fine; the marker still means the truck left.
    assert _dead([_g(1, "1225", "INACTIVE - 1225 MAGAN")]) == [1]


def test_the_reason_says_why():
    reasons = [c["reason"] for c in title_deactivations([_g(1, "1225", "MAGAN")])]
    assert reasons == ["title no longer names a unit"]


def test_title_still_naming_a_unit_is_left_alone():
    assert _dead([_g(1, "1225", "1225 / MAGAN")]) == []


# ---------- a stored unit on the title vetoes the retirement ----------

def test_a_unit_the_parser_cannot_read_is_not_a_missing_unit():
    """The 2026-08-26 regression: a running JRD truck retired for its own name.

    parse_unit could not read a hyphenated letter prefix, so a title with the
    number printed on it read as "no unit at all". The stored unit is now looked
    for directly, which needs no guess about the format.
    """
    assert _dead([_g(1, "T-120", "T-120 QUINTERO, JOHN / LOUISSAINT, JEAN R")]) == []


def test_the_veto_matches_whole_tokens_only():
    # Titles that parse to nothing, so only the veto could save them.
    # "120" inside "21203" is not this group's unit still being on the title...
    assert _dead([_g(1, "120", "MAGAN, MOHAMED 21203")]) == [1]
    # ...and a title naming 1002FT is not a title naming 1002.
    assert _dead([_g(1, "1002", "MAGAN, MOHAMED 1002FT")]) == [1]


def test_a_retired_marker_still_wins_over_the_veto():
    # The fleet leaves the number on these: "INACTIVE - 1225 MAGAN".
    assert _dead([_g(1, "1225", "INACTIVE - 1225 MAGAN")]) == [1]


# ---------- the active list is never consulted here ----------

def test_a_title_naming_some_other_unit_is_never_retired():
    """The rule that retired these was removed: the list is not trustworthy.

    Filed as 1225, title claims 9999. Retiring on "9999 isn't on the list"
    stacked a 79.5%-accurate title parse against a list that omits live trucks,
    and wrote unattended three times a week -- either input being wrong retires
    a running group. If 9999 really is the new truck, title_unit_changes
    re-files it; otherwise nothing happens, which is the right answer.
    """
    assert _dead([_g(1, "1225", "UNIT 9999 / MAGAN")]) == []


def test_a_delisted_unit_is_not_the_sweep_s_business():
    # Retiring a truck for falling off the list belongs to /units, where an
    # admin previews the casualties and confirms them.
    assert _dead([_g(1, "1225", "1225 / MAGAN")]) == []


# ---------- what it refuses to touch ----------

def test_unconfigured_group_is_never_retired():
    # No stored unit means no truck to retire, and its title not parsing is the
    # very question onboarding is waiting to ask an admin.
    assert _dead([_g(1, None, "Dispatch chat")]) == []


def test_unreadable_title_is_not_evidence():
    # _refresh_titles drops groups it couldn't fetch; a blank must never read as
    # "the title lost its unit" and retire a group for being unreachable.
    assert _dead([_g(1, "1225", None)]) == []


def test_already_inactive_group_is_skipped():
    assert _dead([_g(1, "1225", "MAGAN", active=False)]) == []


# ---------- re-file first, then retire ----------

def test_a_renamed_group_is_not_retired_for_losing_its_old_number():
    groups = [_g(1, "1225", "UNIT 1330 / MAGAN")]
    renames = title_unit_changes(groups, ["1330"])
    assert _dead(apply_renames(groups, renames)) == []


def test_rename_and_retire_can_both_happen_in_one_sweep():
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1400", "INACTIVE - B")]
    units = ["1330", "1400"]
    renames = title_unit_changes(groups, units)
    after = apply_renames(groups, renames)
    assert [(r["old"], r["new"]) for r in renames] == [("1225", "1330")]
    assert _dead(after) == [2]


def test_a_collision_leaves_both_groups_alone():
    # Two titles claiming 1330: neither is re-filed (guessing which is right is
    # how two groups end up under one unit), and neither is retired either. An
    # unresolved collision must not turn into a deactivation through the back
    # door.
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1226", "UNIT 1330 / B")]
    renames = title_unit_changes(groups, ["1330"])
    assert renames == []
    assert _dead(apply_renames(groups, renames)) == []


# ---------- the schedule ----------

def _at(y, m, d) -> datetime:
    return datetime(y, m, d, 7, 0)


def test_runs_every_day():
    # 2026-08-10 is a Monday; the whole week is a sweep day now.
    for day in range(10, 17):
        assert title_sweep_due(_at(2026, 8, day), None)


def test_does_not_run_twice_in_one_day():
    # The six-hourly tick hits every day four times over.
    assert not title_sweep_due(_at(2026, 8, 12), date(2026, 8, 12))


def test_a_previous_sweep_does_not_block_the_next_day():
    assert title_sweep_due(_at(2026, 8, 12), date(2026, 8, 10))
