"""A video replying to the bot is inspected even with auto-check off.

Pure: no network. `bot.get_me` is stubbed, so this asserts the reply-matching
rule itself — in particular that *another* bot's message is not a trigger.
"""
import asyncio
import types as pytypes

import pytest

from handlers.groups import pti

OUR_BOT_ID = 424242


class _User:
    def __init__(self, uid, is_bot=True):
        self.id = uid
        self.is_bot = is_bot


class _Msg:
    def __init__(self, reply_to_message=None, from_user=None):
        self.reply_to_message = reply_to_message
        self.from_user = from_user


@pytest.fixture(autouse=True)
def _stub_bot(monkeypatch):
    """Stub bot.get_me() and clear the cached id between tests."""
    calls = []

    async def fake_get_me():
        calls.append(1)
        return pytypes.SimpleNamespace(id=OUR_BOT_ID)

    monkeypatch.setattr(pti.bot, "get_me", fake_get_me)
    monkeypatch.setattr(pti, "_bot_id", None)
    return calls


def _run(msg):
    return asyncio.run(pti._replies_to_bot(msg))


def test_reply_to_our_bot_is_a_trigger():
    reply = _Msg(from_user=_User(OUR_BOT_ID))
    assert _run(_Msg(reply_to_message=reply)) is True


def test_reply_to_a_different_bot_is_not_a_trigger():
    # is_bot alone would wrongly accept this — another bot in the group must
    # not be able to make its own messages an inspection trigger.
    reply = _Msg()
    reply.from_user = _User(999999, is_bot=True)
    assert _run(_Msg(reply_to_message=reply)) is False


def test_reply_to_a_human_is_not_a_trigger():
    reply = _Msg()
    reply.from_user = _User(555, is_bot=False)
    assert _run(_Msg(reply_to_message=reply)) is False


def test_not_a_reply_is_not_a_trigger():
    assert _run(_Msg(reply_to_message=None)) is False


def test_reply_without_a_sender_is_not_a_trigger():
    reply = _Msg()
    reply.from_user = None
    assert _run(_Msg(reply_to_message=reply)) is False


def test_bot_id_is_fetched_once_and_cached(_stub_bot):
    reply = _Msg()
    reply.from_user = _User(OUR_BOT_ID)
    msg = _Msg(reply_to_message=reply)
    assert _run(msg) is True
    assert _run(msg) is True
    assert len(_stub_bot) == 1  # cached, not re-fetched per message


def test_failed_lookup_answers_false_rather_than_raising(monkeypatch):
    async def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(pti.bot, "get_me", boom)
    monkeypatch.setattr(pti, "_bot_id", None)
    reply = _Msg()
    reply.from_user = _User(OUR_BOT_ID)
    # Must degrade to the previous behaviour, never break a message handler.
    assert _run(_Msg(reply_to_message=reply)) is False
