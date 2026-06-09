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


def test_mute_chat_owner_returns_owner(monkeypatch):
    monkeypatch.setattr(
        enforcement.bot, "restrict_chat_member",
        AsyncMock(side_effect=CantRestrictChatOwner("Can't remove chat owner")),
    )
    assert asyncio.run(enforcement.mute_driver(-100123, 555)) is RestrictOutcome.OWNER


def test_mute_success_returns_applied(monkeypatch):
    monkeypatch.setattr(enforcement.bot, "restrict_chat_member", AsyncMock(return_value=True))
    assert asyncio.run(enforcement.mute_driver(-100123, 555)) is RestrictOutcome.APPLIED


def test_enforcement_disabled_only_unmutes(monkeypatch):
    # Default config has enforcement off; the loop should lift restrictions but
    # never mute and never send group/admin reminders.
    assert enforcement.ENFORCEMENT_ENABLED is False

    restrict_perms = []

    async def fake_restrict(group_id, user_id, permissions=None):
        restrict_perms.append(permissions)

    sent = []

    async def fake_send(group_id, text, **kwargs):
        sent.append(text)

    async def fake_get_chat(group_id):
        return type("Chat", (), {"title": "G"})()

    monkeypatch.setattr(enforcement, "get_all_registered_groups", AsyncMock(return_value=[{"group_id": -100}]))
    monkeypatch.setattr(enforcement, "get_drivers", AsyncMock(return_value=[{"user_id": 1, "name": "Bob"}]))
    monkeypatch.setattr(enforcement.bot, "get_chat", fake_get_chat)
    monkeypatch.setattr(enforcement.bot, "restrict_chat_member", fake_restrict)
    monkeypatch.setattr(enforcement.bot, "send_message", fake_send)

    asyncio.run(enforcement.run_compliance_check())

    assert enforcement._FULL_PERMISSIONS in restrict_perms      # unmuted
    assert enforcement._MUTED_PERMISSIONS not in restrict_perms  # never muted
    assert sent == []                                            # no group/admin reminders
