"""`/check` on a video from before the bot was added to the group.

Telegram hands a bot a chat only from the moment it joins, so a reply to
anything older arrives as a placeholder: a message id and nothing else. What
is missing from it is the *sender* as much as the video, so the first guard it
fails is the roster one -- which is how a registered driver's own PTI came
back "not from a registered driver", under a line telling an admin to go and
add them in the panel.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import types

from handlers.groups import pti

GROUP_ID = -1001234567890
DRIVER_UID = 5001
CHAT = {"id": GROUP_ID, "type": "supergroup"}
SENDER = {"id": DRIVER_UID, "is_bot": False, "first_name": "Sean"}


def _placeholder() -> types.Message:
    """What Telegram returns for a message the bot cannot fetch."""
    return types.Message(**{"message_id": 11, "date": 0, "chat": CHAT})


def _readable(**content) -> types.Message:
    payload = {"message_id": 12, "date": 1760000000, "chat": CHAT,
               "from": SENDER}
    payload.update(content)
    return types.Message(**payload)


VIDEO = {"file_id": "abc", "file_unique_id": "u1", "width": 640,
         "height": 480, "duration": 90}


# ---------- what counts as unreadable ----------

def test_a_placeholder_is_unreadable():
    assert pti._reply_is_unreadable(_placeholder()) is True


def test_a_video_is_readable():
    assert pti._reply_is_unreadable(_readable(video=VIDEO)) is False


def test_a_text_message_is_readable():
    assert pti._reply_is_unreadable(_readable(text="good morning")) is False


def test_an_unknown_service_message_is_not_a_placeholder():
    """A newer Bot API service message this aiogram cannot classify.

    It has no content type either, so the sender is the other half of the
    test: a real message names who caused it, a placeholder names nobody.
    Replying `/check` to one is the ordinary "that isn't a video" mistake and
    must keep the ordinary answer.
    """
    service = _readable(boost_added={"boost_count": 1})
    assert service.content_type == types.ContentType.UNKNOWN
    assert pti._reply_is_unreadable(service) is False


# ---------- what the group is told ----------

def _check(monkeypatch, reply, *, registered=True):
    """Run `/check` against `reply` and return (answers, roster lookups)."""
    monkeypatch.setattr(pti, "get_group", AsyncMock(
        return_value={"setup_complete": True, "unit_number": "1216"}))
    roster = AsyncMock(return_value=registered)
    monkeypatch.setattr(pti, "is_registered_driver", roster)
    monkeypatch.setattr(pti, "get_drivers", AsyncMock(return_value=[
        {"user_id": DRIVER_UID, "name": "Sean Taylor"}]))
    run_pti = AsyncMock()
    monkeypatch.setattr(pti, "_run_pti", run_pti)

    answer = AsyncMock()
    message = SimpleNamespace(chat=SimpleNamespace(id=GROUP_ID),
                              reply_to_message=reply, answer=answer)
    asyncio.run(pti.handle_check_group(message))
    return answer, roster, run_pti


def test_the_driver_is_not_blamed_for_a_message_the_bot_cannot_read(monkeypatch):
    answer, roster, run_pti = _check(monkeypatch, _placeholder())

    said = answer.await_args.args[0]
    assert "registered driver" not in said
    assert "admin panel" not in said
    # Checked before the roster, because the roster is what a placeholder
    # fails: there is no sender on it to look up.
    roster.assert_not_awaited()
    run_pti.assert_not_awaited()


def test_it_says_what_to_do_about_it(monkeypatch):
    answer, _, _ = _check(monkeypatch, _placeholder())

    said = answer.await_args.args[0]
    assert pti.OUT_OF_REACH in said
    assert "send the video here again" in said
    assert "/check" in said


def test_a_readable_reply_still_goes_to_the_roster(monkeypatch):
    """The placeholder branch must not swallow the ordinary path."""
    answer, roster, run_pti = _check(monkeypatch, _readable(video=VIDEO))

    roster.assert_awaited()
    run_pti.assert_awaited_once()
    answer.assert_not_awaited()


def test_the_no_reply_refusal_names_the_same_cause(monkeypatch):
    """Two refusals, one cause.

    A `/check` with no reply at all is the same dead end whenever the
    placeholder is not delivered either, and a driver who did reply to the
    morning's video must not read a flat instruction to do what they just did.
    """
    answer, _, run_pti = _check(monkeypatch, None)

    assert pti.OUT_OF_REACH in answer.await_args.args[0]
    run_pti.assert_not_awaited()


def test_an_unconfigured_group_is_answered_first(monkeypatch):
    """The setup refusal still wins -- it is the one the admins were nagged
    about, and it is answered whatever the reply turns out to be."""
    monkeypatch.setattr(pti, "get_group", AsyncMock(return_value=None))
    answer = AsyncMock()
    asyncio.run(pti.handle_check_group(SimpleNamespace(
        chat=SimpleNamespace(id=GROUP_ID), reply_to_message=_placeholder(),
        answer=answer)))

    assert "isn't set up yet" in answer.await_args.args[0]
