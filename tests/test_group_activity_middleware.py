"""What counts as a human message for the dormancy stamp.

Pure classification only (`_is_human`) — no DB, no Telegram. The rule mirrors
scripts/tg_scan.py: skip the bot's own chatter, skip service events.
"""
from aiogram import types

from middlewares.group_activity import _is_human

CHAT = {"id": -1001234567890, "type": "supergroup", "title": "1216 / MARTINEZ"}


def msg(**extra) -> types.Message:
    data = {"message_id": 1, "date": 1, "chat": CHAT,
            "from": {"id": 555, "is_bot": False, "first_name": "Driver"}}
    data.update(extra)
    return types.Message(**data)


def test_driver_text_counts():
    assert _is_human(msg(text="on my way"))


def test_driver_video_counts():
    """The PTI itself is the most important sign of life."""
    assert _is_human(msg(video={"file_id": "v", "file_unique_id": "u",
                                "width": 1, "height": 1, "duration": 30}))


def test_another_bot_does_not_count():
    assert not _is_human(msg(**{"from": {"id": 42, "is_bot": True, "first_name": "SomeBot"}}))


def test_anonymous_admin_counts():
    """Telegram attributes an anonymous admin's post to GroupAnonymousBot, but
    a human typed it — sender_chat is what tells the two apart."""
    assert _is_human(msg(
        **{"from": {"id": 1087968824, "is_bot": True, "first_name": "Group"},
           "sender_chat": CHAT, "text": "hi"}
    ))


def test_service_messages_do_not_count():
    joined = msg(new_chat_members=[{"id": 9, "is_bot": False, "first_name": "New"}])
    left = msg(left_chat_member={"id": 9, "is_bot": False, "first_name": "New"})
    retitled = msg(new_chat_title="1216 / MARTINEZ (INACTIVE)")
    assert not _is_human(joined)
    assert not _is_human(left)
    assert not _is_human(retitled)


def test_pinned_message_does_not_count():
    assert not _is_human(msg(pinned_message=dict(
        message_id=2, date=1, chat=CHAT,
        **{"from": {"id": 555, "is_bot": False, "first_name": "Driver"}}, text="rules"
    )))
