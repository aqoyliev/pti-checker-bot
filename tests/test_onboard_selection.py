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
