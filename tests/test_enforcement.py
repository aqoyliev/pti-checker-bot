"""Regression tests for enforcement edge cases.

The hourly compliance loop must not crash or spam errors when a tracked driver
is the group creator: Telegram forbids a bot from restricting the chat owner
(`CantRestrictChatOwner`), and that should be a benign no-op, not an error.
"""
import asyncio

from aiogram.utils.exceptions import CantRestrictChatOwner

from utils import enforcement


def _raise_owner(*_args, **_kwargs):
    async def _coro():
        raise CantRestrictChatOwner("Can't remove chat owner")
    return _coro()


def test_unmute_chat_owner_is_benign(monkeypatch):
    monkeypatch.setattr(enforcement.bot, "restrict_chat_member", _raise_owner)
    # Returns True (no deregistration) and does not raise.
    assert asyncio.run(enforcement.unmute_driver(-100123, 555)) is True


def test_mute_chat_owner_is_benign(monkeypatch):
    monkeypatch.setattr(enforcement.bot, "restrict_chat_member", _raise_owner)
    assert asyncio.run(enforcement.mute_driver(-100123, 555)) is True
