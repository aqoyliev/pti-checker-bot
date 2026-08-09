"""Regression tests for enforcement edge cases.

The hourly compliance loop must not crash or spam errors when a tracked driver
is the group creator: Telegram forbids a bot from restricting the chat owner
(`CantRestrictChatOwner`), and that should be a benign no-op, not an error.
"""
import asyncio
from unittest.mock import AsyncMock

from aiogram.utils.exceptions import CantRestrictChatOwner

from utils import enforcement
from utils.enforcement import RestrictOutcome


def test_unmute_chat_owner_returns_owner(monkeypatch):
    monkeypatch.setattr(
        enforcement.bot, "restrict_chat_member",
        AsyncMock(side_effect=CantRestrictChatOwner("Can't remove chat owner")),
    )
    # Owner can't be restricted: benign no-op, not a deregistration, not "applied".
    assert asyncio.run(enforcement.unmute_driver(-100123, 555)) is RestrictOutcome.OWNER


def test_unmute_success_returns_applied(monkeypatch):
    monkeypatch.setattr(enforcement.bot, "restrict_chat_member", AsyncMock(return_value=True))
    assert asyncio.run(enforcement.unmute_driver(-100123, 555)) is RestrictOutcome.APPLIED


def test_there_is_no_way_to_mute_a_driver():
    """The rule is structural, not a config flag: no mute helper may exist.

    A muted-permission set or a mute_driver() is all it would take for a future
    change to start restricting drivers again, so their absence is asserted.
    """
    assert not hasattr(enforcement, "mute_driver")
    assert not hasattr(enforcement, "_MUTED_PERMISSIONS")


def _fake_loop_env(monkeypatch, restrict_perms, sent):
    async def fake_restrict(group_id, user_id, permissions=None):
        restrict_perms.append(permissions)

    async def fake_send(group_id, text, **kwargs):
        sent.append(text)

    async def fake_get_chat(group_id):
        return type("Chat", (), {"title": "G"})()

    monkeypatch.setattr(enforcement, "get_all_registered_groups",
                        AsyncMock(return_value=[{"group_id": -100}]))
    monkeypatch.setattr(enforcement, "get_drivers",
                        AsyncMock(return_value=[{"user_id": 1, "name": "Bob"}]))
    monkeypatch.setattr(enforcement.bot, "get_chat", fake_get_chat)
    monkeypatch.setattr(enforcement.bot, "restrict_chat_member", fake_restrict)
    monkeypatch.setattr(enforcement.bot, "send_message", fake_send)


def test_disabled_loop_does_nothing(monkeypatch):
    # Reminders off: no restriction calls and no messages. The loop bails before
    # touching a group, so a bot without Restrict Members rights can't produce a
    # failed call that reads as "group unreachable".
    assert enforcement.ENFORCEMENT_ENABLED is False

    restrict_perms, sent = [], []
    _fake_loop_env(monkeypatch, restrict_perms, sent)

    asyncio.run(enforcement.run_compliance_check())

    assert restrict_perms == []
    assert sent == []


def test_overdue_driver_is_reminded_never_restricted(monkeypatch):
    # With reminders on, an overdue driver gets a message and shows up in the
    # admin report — but the bot still makes no restriction call at all.
    restrict_perms, sent = [], []
    _fake_loop_env(monkeypatch, restrict_perms, sent)
    monkeypatch.setattr(enforcement, "ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(enforcement, "check_driver_compliance",
                        AsyncMock(return_value=(False, "no PTI submitted yet this week")))

    asyncio.run(enforcement.run_compliance_check())

    assert restrict_perms == []
    assert any("overdue" in t for t in sent)
    assert not any("restricted" in t.lower() for t in sent)
