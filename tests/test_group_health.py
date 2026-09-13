"""Telling "the bot was muted here" apart from "the bot was removed".

The two look identical from the outside — nothing reaches the group either way
— and they need opposite answers. A muted bot is a permission an admin can give
back today, and nothing else in the system notices it; a removed bot is already
handled by the unreachable strikes, and alerting on it would mean an alert for
every group anyone ever tidied up.

Pure: no network, no database.
"""
import asyncio

import pytest
from aiogram.utils.exceptions import (
    BadRequest, BotBlocked, BotKicked, ChatNotFound, Unauthorized,
)

from utils import group_health as gh


# ---------- muted ----------

@pytest.mark.parametrize("description", [
    "Bad Request: have no rights to send a message",
    "Bad Request: not enough rights to send text messages to the chat",
    "Bad Request: CHAT_WRITE_FORBIDDEN",
])
def test_a_refused_send_is_a_mute(description):
    assert gh.is_post_denied(BadRequest(description)) is True


# ---------- not muted ----------

def test_a_removed_bot_is_not_a_mute():
    # Unauthorized covers BotKicked and BotBlocked. There is no permission to
    # hand back, so telling an admin to look for one sends them nowhere.
    assert gh.is_post_denied(BotKicked("bot was kicked from the supergroup chat")) is False
    assert gh.is_post_denied(BotBlocked("bot was blocked by the user")) is False
    assert gh.is_post_denied(Unauthorized("Unauthorized")) is False


def test_a_chat_that_cannot_be_found_is_not_a_mute():
    # The local Bot API server answers this for chats it has not seen since its
    # last restart, for groups the bot is perfectly well inside.
    assert gh.is_post_denied(ChatNotFound("chat not found")) is False


def test_an_ordinary_bad_request_is_not_a_mute():
    assert gh.is_post_denied(BadRequest("message is too long")) is False


def test_a_local_failure_is_not_a_mute():
    assert gh.is_post_denied(RuntimeError("have no rights to send a message")) is False


# ---------- the alert fires on the change, not on the state ----------

def _record(monkeypatch, changed: bool):
    sent: list[str] = []
    monkeypatch.setattr(gh, "set_post_blocked",
                        lambda gid, blocked: _async(changed))
    monkeypatch.setattr(gh, "get_group",
                        lambda gid: _async({"group_id": gid, "unit_number": "2570",
                                            "title": "UNIT 2570"}))
    monkeypatch.setattr(gh, "notify_super_admins",
                        lambda text, **kw: _async(sent.append(text) or 1))
    return sent


async def _async(value):
    return value


def test_admins_are_told_when_the_answer_changes(monkeypatch):
    sent = _record(monkeypatch, changed=True)
    asyncio.run(gh.record_post_access(-100, True))
    assert len(sent) == 1
    assert "2570" in sent[0] and "Send Messages" in sent[0]


def test_admins_are_told_when_it_is_fixed(monkeypatch):
    sent = _record(monkeypatch, changed=True)
    asyncio.run(gh.record_post_access(-100, False))
    assert len(sent) == 1
    assert "again" in sent[0]


def test_an_unchanged_answer_says_nothing(monkeypatch):
    # The hourly reminder pass would otherwise re-send the same alert all week.
    sent = _record(monkeypatch, changed=False)
    asyncio.run(gh.record_post_access(-100, True))
    assert sent == []


def test_a_failure_that_is_not_a_mute_records_nothing(monkeypatch):
    sent = _record(monkeypatch, changed=True)
    asyncio.run(gh.note_send_failure(-100, BotKicked("bot was kicked from the chat")))
    assert sent == []


def test_the_label_falls_back_to_the_id(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(gh, "set_post_blocked", lambda gid, blocked: _async(True))
    monkeypatch.setattr(gh, "get_group", lambda gid: _async(None))
    monkeypatch.setattr(gh, "notify_super_admins",
                        lambda text, **kw: _async(sent.append(text) or 1))
    asyncio.run(gh.record_post_access(-100, True))
    assert "-100" in sent[0]
