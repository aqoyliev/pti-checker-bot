"""The onboarding prompt must tell its caller whether an admin actually got it.

A bot cannot open a DM with someone who never started it, so "no admin
reachable" is an ordinary outcome. If start_onboarding swallows that and still
looks successful, the joined group gets no setup instructions and no admin gets
a picker -- the group is silently stranded, with only a log line to show for it.
"""
import asyncio
from unittest.mock import AsyncMock

from aiogram.utils.exceptions import CantInitiateConversation

from handlers.admin import onboard


def _stub_userbot(monkeypatch, members=()):
    monkeypatch.setattr(onboard.userbot, "list_members", AsyncMock(return_value=list(members)))
    monkeypatch.setattr(onboard.userbot, "get_description", AsyncMock(return_value=""))
    # The unit guess is checked against the active-units list, which is a DB
    # read; these tests are about admin reachability, so stub it out.
    monkeypatch.setattr(onboard, "get_active_units", AsyncMock(return_value=set()))
    monkeypatch.setattr(onboard, "get_non_driver_ids", AsyncMock(return_value=set()))


def test_returns_true_when_an_admin_is_reached(monkeypatch):
    _stub_userbot(monkeypatch)
    monkeypatch.setattr(onboard, "_ADMIN_IDS", [7564871221])
    monkeypatch.setattr(onboard.bot, "send_message", AsyncMock(return_value=True))

    assert asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH")) is True


def test_returns_false_when_the_dm_is_blocked(monkeypatch):
    _stub_userbot(monkeypatch)
    monkeypatch.setattr(onboard, "_ADMIN_IDS", [7564871221])
    monkeypatch.setattr(
        onboard.bot, "send_message",
        AsyncMock(side_effect=CantInitiateConversation("bot can't initiate conversation")),
    )

    assert asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH")) is False


def test_returns_false_when_there_are_no_admins(monkeypatch):
    _stub_userbot(monkeypatch)
    monkeypatch.setattr(onboard, "_ADMIN_IDS", [])
    monkeypatch.setattr(onboard.bot, "send_message", AsyncMock(return_value=True))

    assert asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH")) is False


def test_one_reachable_admin_is_enough(monkeypatch):
    _stub_userbot(monkeypatch)
    monkeypatch.setattr(onboard, "_ADMIN_IDS", [111, 222])
    monkeypatch.setattr(
        onboard.bot, "send_message",
        AsyncMock(side_effect=[CantInitiateConversation("blocked"), True]),
    )

    assert asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH")) is True


def test_failed_admin_leaves_no_pending_state(monkeypatch):
    """A prompt nobody received must not leave state behind pretending it did."""
    _stub_userbot(monkeypatch)
    monkeypatch.setattr(onboard, "_ADMIN_IDS", [7564871221])
    monkeypatch.setattr(
        onboard.bot, "send_message",
        AsyncMock(side_effect=CantInitiateConversation("blocked")),
    )
    onboard._pending.clear()

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    assert onboard._pending == {}
