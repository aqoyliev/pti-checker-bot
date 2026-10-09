"""The setup commands belong to the group, not to the admins.

Everyone a setup needs is already in the chat: the driver, whose own message is
the only thing that identifies their account, and whoever read `/check`'s
refusal. Routing it through an admin who is not there is what left groups
unconfigured for weeks while their drivers watched a bot that had stopped
answering -- so as of 2026-10-10 any member may run `/setunit`, `/adddriver`
and `/removedriver`, a bare `/adddriver` stores the driver's Telegram name
rather than refusing, and it adopts the unit the chat title names rather than
asking for a number the group has already written down.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.groups import registration as reg

GROUP_ID = -1001234567890
DRIVER_UID = 5001
PLAIN_TITLE = "Team chat"


def _message(args: str = "", *, reply_name: str | None = "Emile Fleurmond",
             is_bot: bool = False, title: str = PLAIN_TITLE):
    sender = None if reply_name is None else SimpleNamespace(
        id=DRIVER_UID, is_bot=is_bot, full_name=reply_name)
    return SimpleNamespace(
        chat=SimpleNamespace(id=GROUP_ID, title=title),
        from_user=SimpleNamespace(id=9999),
        reply_to_message=SimpleNamespace(from_user=sender),
        get_args=lambda: args,
        reply=AsyncMock(),
    )


def _patch(monkeypatch, *, unit="1216", drivers=()):
    """Stub out every write the commands make."""
    monkeypatch.setattr(reg, "upsert_group", AsyncMock())
    monkeypatch.setattr(reg, "get_group", AsyncMock(
        return_value={"unit_number": unit}))
    monkeypatch.setattr(reg, "get_drivers", AsyncMock(return_value=list(drivers)))
    added = AsyncMock(return_value=True)
    monkeypatch.setattr(reg, "add_driver", added)
    unit_set = AsyncMock()
    monkeypatch.setattr(reg, "set_group_unit", unit_set)
    removed = AsyncMock(return_value=True)
    monkeypatch.setattr(reg, "remove_driver", removed)
    return added, unit_set, removed


# ---------- who may run them ----------

def test_anyone_in_the_group_can_register_a_driver(monkeypatch):
    added, _, _ = _patch(monkeypatch)
    message = _message("Jacques")

    asyncio.run(reg.cmd_add_driver(message))

    added.assert_awaited_once()
    assert added.await_args.args[:2] == (GROUP_ID, DRIVER_UID)


def test_anyone_in_the_group_can_set_the_unit(monkeypatch):
    _, unit_set, _ = _patch(monkeypatch)
    message = _message("1216")

    asyncio.run(reg.cmd_set_unit(message))

    unit_set.assert_awaited_once_with(GROUP_ID, "1216")


def test_anyone_in_the_group_can_remove_a_driver(monkeypatch):
    """Open like the other two: swapping a driver is part of keeping a group
    right, and the admin it used to wait for is not in the chat."""
    _, _, removed = _patch(monkeypatch)
    message = _message()

    asyncio.run(reg.cmd_remove_driver(message))

    removed.assert_awaited_once_with(GROUP_ID, DRIVER_UID)


def test_no_admin_gate_is_left_in_the_module():
    """The gate is gone, not bypassed -- a helper left behind invites a
    re-wiring that the fleet asked not to have."""
    assert not hasattr(reg, "_admin_only")
    assert not hasattr(reg, "_NOT_ADMIN")


# ---------- the name is optional ----------

def test_a_bare_adddriver_stores_the_telegram_name(monkeypatch):
    """Refusing left the group with no driver, which is what stops /check."""
    added, _, _ = _patch(monkeypatch)
    message = _message("", reply_name="emile fleurmond")

    asyncio.run(reg.cmd_add_driver(message))

    # Stored in the one shape every report prints, same as a typed name.
    assert added.await_args.args[2] == "Emile Fleurmond"


def test_a_typed_name_still_wins(monkeypatch):
    added, _, _ = _patch(monkeypatch)
    message = _message("ZAMA, EMILE", reply_name="Emile")

    asyncio.run(reg.cmd_add_driver(message))

    assert added.await_args.args[2] == "Zama Emile"


def test_a_bare_mention_falls_through_to_the_profile_name(monkeypatch):
    """`/adddriver @emile` names nobody the roster can store."""
    added, _, _ = _patch(monkeypatch)
    message = _message("@emile_f", reply_name="Emile Fleurmond")

    asyncio.run(reg.cmd_add_driver(message))

    assert added.await_args.args[2] == "Emile Fleurmond"


# ---------- the unit the title already names ----------

def test_adddriver_adopts_the_unit_from_the_title(monkeypatch):
    """One command finishes the setup. The parse is a guess, so the reply says
    where the number came from and offers /setunit."""
    _, unit_set, _ = _patch(monkeypatch, unit=None)
    message = _message("", title="1216 QUINTERO, JOHN / ZAMA, EMILE")

    asyncio.run(reg.cmd_add_driver(message))

    unit_set.assert_awaited_once_with(GROUP_ID, "1216")
    said = message.reply.await_args.args[0]
    assert "title" in said
    assert "/setunit" in said


def test_a_stored_unit_outranks_the_title(monkeypatch):
    """The title is the fallback, never a correction -- re-filing a group is
    the daily sweep's job, under its own collision rules."""
    _, unit_set, _ = _patch(monkeypatch, unit="1216")
    message = _message("", title="9999 SOMEBODY ELSE")

    asyncio.run(reg.cmd_add_driver(message))

    unit_set.assert_awaited_once_with(GROUP_ID, "1216")


def test_a_title_with_no_number_still_asks_for_the_unit(monkeypatch):
    added, unit_set, _ = _patch(monkeypatch, unit=None)
    message = _message("", title="Dispatch and safety")

    asyncio.run(reg.cmd_add_driver(message))

    added.assert_awaited_once()
    unit_set.assert_not_awaited()
    assert "/setunit" in message.reply.await_args.args[0]


# ---------- the guards that stay ----------

def test_the_bot_is_still_not_a_driver(monkeypatch):
    """Replying to one of the bot's own setup messages used to register it."""
    added, _, _ = _patch(monkeypatch)
    message = _message("", reply_name="PTI Checker Bot", is_bot=True)

    asyncio.run(reg.cmd_add_driver(message))

    added.assert_not_awaited()


def test_adddriver_needs_a_reply(monkeypatch):
    """The reply is the whole point: it is what identifies the account."""
    added, _, _ = _patch(monkeypatch)
    message = _message("Jacques", reply_name=None)

    asyncio.run(reg.cmd_add_driver(message))

    added.assert_not_awaited()
    assert "/adddriver" in message.reply.await_args.args[0]


def test_a_third_driver_is_refused_and_told_how_to_swap(monkeypatch):
    added, _, _ = _patch(monkeypatch, drivers=[
        {"user_id": 1, "name": "Zama Emile"},
        {"user_id": 2, "name": "Fleurmond Jacques"},
    ])
    message = _message("Someone Else")

    asyncio.run(reg.cmd_add_driver(message))

    added.assert_not_awaited()
    assert "/removedriver" in message.reply.await_args.args[0]
