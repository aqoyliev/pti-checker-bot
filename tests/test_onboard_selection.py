"""Driver picks are numbered in tap order, not ticked identically.

Group titles pair two drivers whose names often look alike ("SANON ARLETTE /
DATTUS LIVENSTONN"), and picking the wrong row silently stores the wrong
user_id -- which is the whole point of the picker. Numbering the picks makes a
mis-tap visible before Save.
"""
import asyncio
from unittest.mock import AsyncMock

from handlers.admin import onboard
from utils.userbot import Member


def _member(user_id, name):
    return Member(user_id=user_id, name=name, username=None, is_bot=False)


ARLETTE = _member(101, "SANON ARLETTE")
LIVENSTONN = _member(202, "DATTUS LIVENSTONN")
THIRD = _member(303, "OTHER PERSON")


def _state(selected):
    return {
        "title": "test 1234 SANON ARLETTE / DATTUS LIVENSTONN",
        "description": "",
        "unit": "1234",
        "unit_source": "title",
        "retired_marker": False,
        "members": [ARLETTE, LIVENSTONN, THIRD],
        "selected": list(selected),
    }


def _labels_for(selected):
    onboard._pending[onboard._key(1, -100)] = _state(selected)
    kb = onboard._keyboard(1, -100)
    return [b.text for row in kb.inline_keyboard for b in row]


def test_nothing_selected_shows_empty_boxes():
    labels = _labels_for([])
    assert labels[0].startswith(onboard._UNPICKED)
    assert labels[1].startswith(onboard._UNPICKED)


def test_first_pick_is_numbered_one():
    labels = _labels_for([ARLETTE.user_id])
    assert labels[0].startswith("1️⃣")
    assert labels[1].startswith(onboard._UNPICKED)


def test_second_pick_is_numbered_two():
    labels = _labels_for([ARLETTE.user_id, LIVENSTONN.user_id])
    assert labels[0].startswith("1️⃣")
    assert labels[1].startswith("2️⃣")


def test_numbering_follows_tap_order_not_row_order():
    # Tapping the second name first makes it 1️⃣.
    labels = _labels_for([LIVENSTONN.user_id, ARLETTE.user_id])
    assert labels[0].startswith("2️⃣")
    assert labels[1].startswith("1️⃣")


def test_deselecting_the_first_renumbers_the_rest():
    # The marker is derived from the list, so removing a pick must not leave a
    # gap like "▫️ / 2️⃣" with no 1️⃣ on screen.
    labels = _labels_for([LIVENSTONN.user_id])
    assert labels[0].startswith(onboard._UNPICKED)
    assert labels[1].startswith("1️⃣")


def test_body_lists_the_picks_in_order():
    onboard._pending[onboard._key(1, -100)] = _state(
        [LIVENSTONN.user_id, ARLETTE.user_id])
    text = onboard._text(1, -100)
    assert "1️⃣ DATTUS LIVENSTONN" in text
    assert "2️⃣ SANON ARLETTE" in text
    assert "Selected: 2/2" in text


def test_marker_falls_back_when_more_picks_than_ordinals():
    many = list(range(len(onboard._ORDINALS) + 1))
    assert onboard._marker(many, many[-1]) == "✅"


# --- unit checked against the active-units list -------------------------

def test_guess_survives_when_it_is_an_active_unit(monkeypatch):
    monkeypatch.setattr(onboard, "get_active_units", AsyncMock(return_value={"1234", "1216"}))
    assert asyncio.run(onboard._checked_guess("1234 SANON ARLETTE", "")) == ("1234", "title")


def test_guess_is_dropped_when_the_unit_is_retired(monkeypatch):
    # The title still names a truck that has left the fleet, so the admin is
    # shown "not found" rather than a confidently wrong unit.
    monkeypatch.setattr(onboard, "get_active_units", AsyncMock(return_value={"1216"}))
    assert asyncio.run(onboard._checked_guess("1234 SANON ARLETTE", "")) == (None, "")


def test_check_is_skipped_while_no_list_exists(monkeypatch):
    # Empty table = no list supplied yet. Don't reject every unit on day one.
    monkeypatch.setattr(onboard, "get_active_units", AsyncMock(return_value=set()))
    assert asyncio.run(onboard._checked_guess("1234 SANON ARLETTE", "")) == ("1234", "title")


def test_no_guess_never_consults_the_list(monkeypatch):
    called = AsyncMock(return_value={"1234"})
    monkeypatch.setattr(onboard, "get_active_units", called)
    assert asyncio.run(onboard._checked_guess("SANON ARLETTE", "")) == (None, "")
    called.assert_not_awaited()


# --- known non-drivers hidden from the picker ---------------------------
# Dispatchers and safety staff sit in many driver groups, so once an admin has
# passed over someone they should stop being offered. The exclusion must stay
# reversible: it is fleet-wide, so a mistake would otherwise hide a real driver
# in every group forever.

def _state_with_hidden(hidden, selected=(), show_all=False):
    st = _state(selected)
    st["hidden"] = set(hidden)
    st["show_all"] = show_all
    return st


def test_known_non_drivers_are_hidden():
    st = _state_with_hidden({THIRD.user_id})
    assert [m.user_id for m in onboard._visible(st)] == [ARLETTE.user_id,
                                                         LIVENSTONN.user_id]
    assert onboard._hidden_count(st) == 1


def test_show_all_reveals_them():
    st = _state_with_hidden({THIRD.user_id}, show_all=True)
    assert len(onboard._visible(st)) == 3
    assert onboard._hidden_count(st) == 0


def test_a_hidden_person_who_is_selected_stays_visible():
    # Otherwise a stale exclusion would make a current pick vanish from the
    # list while still counting toward the saved drivers.
    st = _state_with_hidden({THIRD.user_id}, selected=[THIRD.user_id])
    assert THIRD.user_id in [m.user_id for m in onboard._visible(st)]


def test_nothing_hidden_when_the_list_is_empty():
    st = _state_with_hidden(set())
    assert len(onboard._visible(st)) == 3
    assert onboard._hidden_count(st) == 0


def test_hidden_people_are_not_re_marked_on_save():
    # Save records the people who were on screen and passed over. Someone
    # hidden was never judged in this group, so they must not be swept in as a
    # fresh decision.
    st = _state_with_hidden({THIRD.user_id}, selected=[ARLETTE.user_id])
    assert onboard._passed_over(st) == [(LIVENSTONN.user_id, LIVENSTONN.label)]


# --- the roster is filtered before it is sliced --------------------------
# The keyboard holds MEMBER_BUTTONS buttons, but that cap is a display limit.
# Applying it to the roster on the way in meant a group whose first 40 members
# were all known non-drivers produced a prompt with no names on it at all --
# the drivers were at position 41+ and had been dropped before the non-driver
# filter ever ran. Observed live: "40 known non-driver(s) hidden", zero buttons.


def _big_state(hidden=(), selected=(), extra=0):
    """A roster longer than the button cap, whose leading members are hidden."""
    staff = [_member(1000 + i, f"STAFF {i}") for i in range(onboard.MEMBER_BUTTONS)]
    drivers = [_member(2000 + i, f"DRIVER {i}") for i in range(2 + extra)]
    st = _state(selected)
    st["members"] = staff + drivers
    st["hidden"] = set(hidden) or {m.user_id for m in staff}
    st["show_all"] = False
    return st


def test_drivers_past_the_button_cap_are_still_offered():
    st = _big_state()
    shown = [m.user_id for m in onboard._shown(st)]
    assert shown == [2000, 2001], "the two drivers should be the only buttons"


def test_the_hidden_count_still_counts_the_whole_roster():
    st = _big_state()
    assert onboard._hidden_count(st) == onboard.MEMBER_BUTTONS


def test_buttons_are_capped_at_the_keyboard_limit():
    # Nothing hidden, roster longer than the cap: the extra members are cut
    # from the keyboard, not from the state.
    st = _big_state(hidden={-1}, extra=10)
    assert len(st["members"]) > onboard.MEMBER_BUTTONS
    assert len(onboard._shown(st)) == onboard.MEMBER_BUTTONS
    assert onboard._overflow_count(st) == len(st["members"]) - onboard.MEMBER_BUTTONS


def test_members_past_the_cap_are_not_marked_as_non_drivers():
    # They were never displayed, so Save must not record them as passed over --
    # that exclusion is fleet-wide and would hide them in every other group.
    st = _big_state(hidden={-1}, extra=10, selected=[2000])
    passed = {uid for uid, _ in onboard._passed_over(st)}
    assert 2000 not in passed, "the pick is not passed over"
    assert not passed & {m.user_id for m in st["members"][onboard.MEMBER_BUTTONS:]}


def test_an_all_hidden_roster_says_so_instead_of_showing_nothing():
    onboard._pending[onboard._key(1, -100)] = _big_state(
        hidden={m.user_id for m in _big_state()["members"]})
    text = onboard._text(1, -100)
    assert "nobody to pick" in text
    assert "Show hidden" in text


# --- two-column layout ---------------------------------------------------
# A staff-heavy group runs to dozens of names; one button per row pushes Save
# off the bottom of the screen.

def _rows_for(selected, hidden=(), show_all=False):
    st = _state(selected)
    st["hidden"] = set(hidden)
    st["show_all"] = show_all
    onboard._pending[onboard._key(1, -100)] = st
    kb = onboard._keyboard(1, -100)
    return [[b.text for b in row] for row in kb.inline_keyboard]


def test_members_are_laid_out_two_per_row():
    rows = _rows_for([])
    assert len(rows[0]) == 2
    assert rows[0][0].endswith("SANON ARLETTE")
    assert rows[0][1].endswith("DATTUS LIVENSTONN")


def test_an_odd_member_count_leaves_a_single_button_last():
    # Three members: two on the first row, one alone on the second.
    rows = _rows_for([])
    assert len(rows[1]) == 1
    assert rows[1][0].endswith("OTHER PERSON")


def test_change_unit_and_save_share_a_row():
    rows = _rows_for([])
    pair = [r for r in rows if len(r) == 2 and any("Save" in b for b in r)]
    assert pair, "Change unit and Save should sit on one row"
    assert any("Change unit" in b for b in pair[0])


def test_refresh_and_skip_stay_full_width():
    rows = _rows_for([])
    for label in ("🔄 Refresh members", "✖️ Skip this group"):
        assert [label] in rows, f"{label} should have a row to itself"


def test_labels_are_trimmed_for_the_narrower_button():
    long_name = onboard._marker([], 999) + " " + "X" * 80
    assert len(long_name[:onboard.LABEL_CHARS]) == onboard.LABEL_CHARS
    rows = _rows_for([])
    for row in rows:
        for text in row:
            assert len(text) <= max(onboard.LABEL_CHARS, 24)
