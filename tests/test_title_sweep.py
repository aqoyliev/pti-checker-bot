"""The automatic title sweep: what it retires, and when it runs.

Pure: no DB, no Telegram. The sweep writes without an admin confirming, so the
guards on *what it declines to touch* are the whole test surface.
"""
from datetime import date, datetime

from handlers.admin.units import (
    apply_reactivations,
    apply_renames,
    title_deactivations,
    title_reactivations,
    title_sweep_due,
    title_unit_changes,
)


def _g(gid: int, unit, title, active=True, by=None) -> dict:
    return {"group_id": gid, "unit_number": unit, "title": title,
            "is_active": active, "deactivated_by": by}


def _off(gid: int, unit, title, by="title") -> dict:
    """An inactive group, retired by the title sweep unless said otherwise."""
    return _g(gid, unit, title, active=False, by=by)


def _dead(groups: list[dict]) -> list[int]:
    return [c["group"]["group_id"] for c in title_deactivations(groups)]


def _back(groups: list[dict], **kw) -> list[tuple[int, str]]:
    return [(r["group"]["group_id"], r["unit"]) for r in title_reactivations(groups, **kw)]


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
    renames = title_unit_changes(groups)
    assert _dead(apply_renames(groups, renames)) == []


def test_rename_and_retire_can_both_happen_in_one_sweep():
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1400", "INACTIVE - B")]
    renames = title_unit_changes(groups)
    after = apply_renames(groups, renames)
    assert [(r["old"], r["new"]) for r in renames] == [("1225", "1330")]
    assert _dead(after) == [2]


def test_a_collision_leaves_both_groups_alone():
    # Two titles claiming 1330: neither is re-filed (guessing which is right is
    # how two groups end up under one unit), and neither is retired either. An
    # unresolved collision must not turn into a deactivation through the back
    # door.
    groups = [_g(1, "1225", "UNIT 1330 / A"), _g(2, "1226", "UNIT 1330 / B")]
    renames = title_unit_changes(groups)
    assert renames == []
    assert _dead(apply_renames(groups, renames)) == []


# ---------- what comes back ----------
#
# The mirror image of the retirement: a retired group whose title names a unit
# again is revived. Before 2026-09-18 nothing reactivated at all, so a truck
# whose group lost its number and got it back stayed off the reports for good.

def test_a_title_naming_the_stored_unit_again_revives_the_group():
    assert _back([_off(1, "1225", "1225 / MAGAN")]) == [(1, "1225")]


def test_a_title_naming_another_unit_revives_under_that_unit():
    # The chat was handed to another truck while it was off: it comes back
    # under the number the title names now, not the one it was retired with.
    assert _back([_off(1, "1225", "UNIT 1330 / MAGAN")]) == [(1, "1330")]


def test_the_stored_unit_on_the_title_wins_over_the_parser():
    # Same veto as the retirement: "T-120" is on the title, whatever parse_unit
    # would have made of it.
    assert _back([_off(1, "T-120", "T-120 QUINTERO, JOHN")]) == [(1, "T-120")]


def test_a_retired_marker_keeps_the_group_off():
    assert _back([_off(1, "1225", "INACTIVE - 1225 MAGAN")]) == []


def test_a_title_with_no_unit_keeps_the_group_off():
    assert _back([_off(1, "1225", "MAGAN, MOHAMED")]) == []


def test_unconfigured_group_is_never_revived():
    # No stored unit: never onboarded, so there is nothing to bring back.
    assert _back([_off(1, None, "UNIT 1330 / MAGAN")]) == []


def test_unreadable_title_is_no_evidence_for_revival_either():
    assert _back([_off(1, "1225", None)]) == []


def test_an_active_group_is_not_a_revival():
    assert _back([_g(1, "1225", "1225 / MAGAN")]) == []


def test_a_unit_held_by_an_active_group_blocks_the_revival():
    # Two groups under one number is a broken compliance denominator -- the
    # same collision rule the rename applies.
    groups = [_off(1, "1225", "1225 / OLD"), _g(2, "1225", "1225 / NEW")]
    assert _back(groups) == []


def test_two_revivals_claiming_one_unit_are_both_skipped():
    groups = [_off(1, "1225", "UNIT 1330 / A"), _off(2, "1330", "1330 / B")]
    assert _back(groups) == []


# ---------- whose retirement the sweep may reverse ----------

def test_a_panel_deactivation_is_never_reversed():
    # An admin's own decision about a chat whose title may say anything;
    # reversing it every morning would make the button useless. Not even
    # /titlecheck offers it -- the panel has its own Reactivate.
    groups = [_off(1, "1225", "1225 / MAGAN", by="panel")]
    assert _back(groups) == []
    assert _back(groups, unattended=True) == []


def test_the_unattended_sweep_reverses_only_its_own_and_unreachable():
    # 'title': its own retirement. 'unreachable': three failed sends, which the
    # local Bot API server produces for chats it merely forgot -- a title read
    # fresh from that very chat is the proof the alarm was false.
    groups = [_off(1, "1225", "1225 / A", by="title"),
              _off(2, "1226", "1226 / B", by="unreachable"),
              _off(3, "1227", "1227 / C", by=None)]
    assert _back(groups, unattended=True) == [(1, "1225"), (2, "1226")]


def test_titlecheck_also_offers_the_legacy_backlog():
    # Retired before the reason was recorded: a person confirms each one.
    groups = [_off(3, "1227", "1227 / C", by=None)]
    assert _back(groups) == [(3, "1227")]


# ---------- revive first, then re-file, then retire ----------

def test_a_revived_group_holds_its_unit_against_a_rename():
    # Group 2's title now claims 1225, the very number group 1 comes back
    # under. Group 1 is active from the first pass on, so the rename collides
    # with it instead of landing beside it.
    groups = [_off(1, "1225", "1225 / A"), _g(2, "1400", "UNIT 1225 / B")]
    revives = title_reactivations(groups, unattended=True)
    after = apply_reactivations(groups, revives)
    assert [(g["group_id"], g["is_active"], g["unit_number"]) for g in after] == \
        [(1, True, "1225"), (2, True, "1400")]
    assert title_unit_changes(after) == []


def test_a_revived_group_is_not_retired_in_the_same_sweep():
    groups = [_off(1, "1225", "UNIT 1330 / A")]
    after = apply_reactivations(groups, title_reactivations(groups, unattended=True))
    assert after[0]["unit_number"] == "1330"
    assert _dead(after) == []
    assert title_unit_changes(after) == []


def test_apply_reactivations_leaves_other_groups_untouched():
    groups = [_off(1, "1225", "1225 / A"), _off(2, "1226", "B"), _g(3, "1227", "1227 / C")]
    after = apply_reactivations(groups, title_reactivations(groups))
    assert [(g["group_id"], g["is_active"]) for g in after] == [(1, True), (2, False), (3, True)]
    assert after[1] is groups[1]


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
