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
