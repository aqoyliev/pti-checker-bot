"""The group is told when its setup lands, and only then.

A driver who ran /check too early was told the fleet admins had been asked to
assign the unit and the drivers, and that /check would work once they had.
Nothing ever came back to say they had, so these pin the message that closes
that loop -- and, just as importantly, the three cases that must stay silent: a
correction to a group that already works, a half-done setup, and a write that
failed.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.utils.exceptions import BadRequest

from handlers.admin import onboard
from utils import setup_notice

GROUP = -100500
DRIVERS = [{"user_id": 8063167928, "name": "M Mgn"},
           {"user_id": 6066541941, "name": "Noor Dubat"}]


def _wire(monkeypatch, *, unit="1216", drivers=DRIVERS):
    """Stand in for the group's row and the bot's send."""
    monkeypatch.setattr(setup_notice, "get_group", AsyncMock(
        return_value={"unit_number": unit} if unit is not None else None))
    monkeypatch.setattr(setup_notice, "get_drivers",
                        AsyncMock(return_value=list(drivers)))
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(setup_notice.bot, "send_message", sent)
    return sent


# ---------- what counts as set up ----------

def test_a_group_with_a_unit_and_a_driver_is_usable(monkeypatch):
    _wire(monkeypatch)
    assert asyncio.run(setup_notice.is_usable(GROUP)) is True


def test_a_unit_with_no_drivers_is_not_usable(monkeypatch):
    """`set_group_unit` flips `setup_complete` on its own, so the flag says yes
    here -- and every video in that chat would still belong to nobody."""
    _wire(monkeypatch, drivers=[])
    assert asyncio.run(setup_notice.is_usable(GROUP)) is False


def test_drivers_with_no_unit_are_not_usable(monkeypatch):
    _wire(monkeypatch, unit="")
    assert asyncio.run(setup_notice.is_usable(GROUP)) is False


def test_a_group_with_no_row_at_all_is_not_usable(monkeypatch):
    _wire(monkeypatch, unit=None)
    assert asyncio.run(setup_notice.is_usable(GROUP)) is False


# ---------- the message ----------

def test_the_notice_names_the_unit_and_tags_every_driver(monkeypatch):
    sent = _wire(monkeypatch)

    asyncio.run(setup_notice.tell_the_group(GROUP, setup_notice.FROM_AN_ADMIN))

    chat_id, text = sent.await_args.args[0], sent.await_args.args[1]
    assert chat_id == GROUP
    assert "1216" in text
    # Tagged, not merely named: the label stays the fleet's own name, and the
    # tag is what puts the message in front of the person it registered -- the
    # one reader who can tell that the wrong name landed on them.
    assert 'tg://user?id=8063167928' in text and "M Mgn" in text
    assert 'tg://user?id=6066541941' in text and "Noor Dubat" in text


def test_the_notice_answers_the_refusal_the_drivers_were_given(monkeypatch):
    """"Once that's done, /check will work here" is the message this closes."""
    sent = _wire(monkeypatch)

    asyncio.run(setup_notice.tell_the_group(GROUP, setup_notice.FROM_AN_ADMIN))

    assert "/check works here now" in sent.await_args.args[1]


def test_the_notice_asks_the_group_for_nothing(monkeypatch):
    """A statement, not a request: drivers are never asked to configure
    anything, so there is nothing here to tap or to type."""
    sent = _wire(monkeypatch)

    asyncio.run(setup_notice.tell_the_group(GROUP, setup_notice.FROM_AN_ADMIN))

    assert "/onboard" not in sent.await_args.args[1]
    assert "reply_markup" not in sent.await_args.kwargs


def test_the_notice_says_which_source_the_picks_came_from(monkeypatch):
    """The drivers are the only people who can tell a name went to the wrong
    account, and judging it starts with knowing what claimed it."""
    sent = _wire(monkeypatch)

    asyncio.run(setup_notice.tell_the_group(GROUP, setup_notice.FROM_ABOUT_TEXT))
    automatic = sent.await_args.args[1]
    asyncio.run(setup_notice.tell_the_group(GROUP, setup_notice.FROM_AN_ADMIN))
    by_hand = sent.await_args.args[1]

    assert "About text" in automatic
    assert "About text" not in by_hand
    assert "admin" in by_hand


def test_a_drivers_name_is_escaped(monkeypatch):
    """Every name here was typed by a person, and the whole message is HTML."""
    sent = _wire(monkeypatch, drivers=[{"user_id": 7, "name": "A & B <boss>"}])

    asyncio.run(setup_notice.tell_the_group(GROUP, setup_notice.FROM_AN_ADMIN))

    text = sent.await_args.args[1]
    assert "A &amp; B &lt;boss&gt;" in text
    assert "<boss>" not in text


def test_a_group_the_bot_cannot_post_in_is_recorded_not_raised(monkeypatch):
    """The setup stands -- the write already happened and the admin has been
    told. But this is often the bot's first real message in that chat, so it
    is the first chance to notice it cannot speak there."""
    sent = _wire(monkeypatch)
    sent.side_effect = BadRequest("have no rights to send a message")
    noted = AsyncMock()
    monkeypatch.setattr(setup_notice, "note_send_failure", noted)

    asyncio.run(setup_notice.tell_the_group(GROUP, setup_notice.FROM_AN_ADMIN))

    assert noted.await_args.args[0] == GROUP


# ---------- only on the transition ----------

def test_it_announces_when_the_group_becomes_usable(monkeypatch):
    sent = _wire(monkeypatch)

    asyncio.run(setup_notice.announce_if_now_usable(
        GROUP, was_usable=False, source=setup_notice.FROM_AN_ADMIN))

    sent.assert_awaited_once()


def test_a_correction_to_a_working_group_says_nothing(monkeypatch):
    """Those drivers have already been told. A notice per correction turns the
    one message that matters into traffic."""
    sent = _wire(monkeypatch)

    asyncio.run(setup_notice.announce_if_now_usable(
        GROUP, was_usable=True, source=setup_notice.FROM_AN_ADMIN))

    sent.assert_not_awaited()


def test_half_a_setup_says_nothing(monkeypatch):
    """A unit with no driver yet is the panel's ordinary intermediate state:
    the admin sets the unit, then searches the roster. The group hears about it
    when the driver lands, not before."""
    sent = _wire(monkeypatch, drivers=[])

    asyncio.run(setup_notice.announce_if_now_usable(
        GROUP, was_usable=False, source=setup_notice.FROM_AN_ADMIN))

    sent.assert_not_awaited()


# ---------- the picker's Save ----------

ADMIN = 7564871221


def _save_call(monkeypatch, *, selected=(8063167928, 6066541941),
               already_usable=False):
    """Stub the picker's Save down to the writes and the sends.

    The group's row is stubbed off what the stubbed writes recorded, because
    the transition rule reads it on both sides: a static stub would make the
    group look set up before Save ever ran.
    """
    stored = {"unit": "1216" if already_usable else None,
              "drivers": list(DRIVERS) if already_usable else []}
    monkeypatch.setattr(setup_notice, "get_group", AsyncMock(
        side_effect=lambda gid: {"unit_number": stored["unit"]}))
    monkeypatch.setattr(setup_notice, "get_drivers", AsyncMock(
        side_effect=lambda gid: list(stored["drivers"])))

    onboard._pending[onboard._key(ADMIN, GROUP)] = {
        "title": "UNIT 1216 SMITH",
        "description": "",
        "unit": "1216",
        "unit_source": "title",
        "retired_marker": False,
        "members": [SimpleNamespace(user_id=uid, label=f"Name {uid}", is_bot=False)
                    for uid in selected],
        "hidden": set(),
        "show_all": False,
        "selected": list(selected),
    }
    monkeypatch.setattr(onboard, "unmark_non_drivers", AsyncMock())
    monkeypatch.setattr(onboard, "mark_non_drivers", AsyncMock(return_value=0))
    monkeypatch.setattr(onboard, "get_drivers", AsyncMock(return_value=[]))
    monkeypatch.setattr(onboard, "set_group_unit", AsyncMock(
        side_effect=lambda gid, unit: stored.__setitem__("unit", unit)))
    monkeypatch.setattr(onboard, "replace_drivers", AsyncMock(
        side_effect=lambda gid, drivers: stored.__setitem__(
            "drivers", list(drivers))))
    return SimpleNamespace(
        data=f"ob:s:{GROUP}:0",
        from_user=SimpleNamespace(id=ADMIN),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )


def test_saving_the_picker_tells_the_group(monkeypatch):
    """The DM prompt is what the early /check pointed the drivers at. Until
    now pressing Save on it answered the admin and nobody else."""
    sent = _wire(monkeypatch)
    call = _save_call(monkeypatch)

    asyncio.run(onboard.on_onboard_click(call, SimpleNamespace()))

    group_posts = [c for c in sent.await_args_list if c.args[0] == GROUP]
    assert len(group_posts) == 1
    assert "1216" in group_posts[0].args[1]


def test_saving_an_edit_of_a_working_group_tells_it_nothing(monkeypatch):
    """Save is also the Edit path for a group that configured itself, and a
    correction is not news to a chat that has already been told."""
    sent = _wire(monkeypatch)
    call = _save_call(monkeypatch, already_usable=True)

    asyncio.run(onboard.on_onboard_click(call, SimpleNamespace()))

    assert [c for c in sent.await_args_list if c.args[0] == GROUP] == []


def test_the_admin_is_answered_before_the_group_is(monkeypatch):
    """The admin's confirmation is the record of what was written, and it must
    not wait on a send into a group that may be muted."""
    sent = _wire(monkeypatch)
    call = _save_call(monkeypatch)

    asyncio.run(onboard.on_onboard_click(call, SimpleNamespace()))

    call.message.edit_text.assert_awaited_once()
    sent.assert_awaited_once()
