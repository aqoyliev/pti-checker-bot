"""The onboarding prompt must tell its caller whether an admin actually got it.

A bot cannot open a DM with someone who never started it, so "no admin
reachable" is an ordinary outcome. If start_onboarding swallows that and still
looks successful, the joined group gets no setup instructions and no admin gets
a picker -- the group is silently stranded, with only a log line to show for it.
"""
import asyncio
from unittest.mock import AsyncMock

from aiogram.utils.exceptions import CantInitiateConversation, ChatNotFound

from handlers.admin import onboard
from utils.userbot import Member


def _stub_userbot(monkeypatch, members=()):
    monkeypatch.setattr(onboard.userbot, "list_members", AsyncMock(return_value=list(members)))
    monkeypatch.setattr(onboard.userbot, "get_description", AsyncMock(return_value=""))
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


# --- rebuilding a prompt whose process is gone ---------------------------
# _pending lives in memory, so every deploy strands open prompts. With each
# group now prompted only once, "expired" would be a dead end rather than an
# inconvenience.

def test_rebuild_recreates_usable_state(monkeypatch):
    _stub_userbot(monkeypatch, members=[
        Member(user_id=1, name="Roberto Pessanha", username=None, is_bot=False),
        Member(user_id=2, name="Monica Pessanha", username=None, is_bot=False),
    ])
    monkeypatch.setattr(onboard.bot, "get_chat", AsyncMock(
        return_value=type("Chat", (), {"title": "1338 - PESSANHA"})()))
    onboard._pending.clear()

    st = asyncio.run(onboard._rebuild_pending(7564871221, -100))

    assert st is not None
    assert st["title"] == "1338 - PESSANHA"
    assert [m.user_id for m in st["members"]] == [1, 2]
    assert st["selected"] == []
    assert onboard._key(7564871221, -100) in onboard._pending


def test_rebuild_gives_up_when_the_chat_is_unreachable(monkeypatch):
    _stub_userbot(monkeypatch)
    monkeypatch.setattr(onboard.bot, "get_chat",
                        AsyncMock(side_effect=ChatNotFound("chat not found")))
    onboard._pending.clear()

    assert asyncio.run(onboard._rebuild_pending(7564871221, -100)) is None
    assert onboard._pending == {}
