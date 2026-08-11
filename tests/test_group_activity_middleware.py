"""What counts as a human message.

Pure: no DB. This predicate decides whether a group looks alive, so the two
exclusions (bots, service events) and the one inclusion (anonymous admins) are
the cases that matter.
"""
from aiogram import types

from middlewares.group_activity import is_human


class _User:
    def __init__(self, is_bot=False):
        self.is_bot = is_bot


class _Msg:
    def __init__(self, content_type, from_user=None, sender_chat=None):
        self.content_type = content_type
        self.from_user = from_user
        self.sender_chat = sender_chat


def test_a_person_talking_counts():
    assert is_human(_Msg(types.ContentType.TEXT, _User())) is True


def test_a_driver_posting_a_video_counts():
    assert is_human(_Msg(types.ContentType.VIDEO, _User())) is True


def test_the_bots_own_messages_do_not_count():
    # PTI results and reminders are the loudest traffic in a dead group;
    # counting them would make every group the bot nags look alive.
    assert is_human(_Msg(types.ContentType.TEXT, _User(is_bot=True))) is False


def test_anonymous_admin_counts_as_human():
    # Telegram attributes these to GroupAnonymousBot (is_bot) *with* sender_chat
    # set. A real bot has no sender_chat.
    msg = _Msg(types.ContentType.TEXT, _User(is_bot=True), sender_chat=object())
    assert is_human(msg) is True


def test_service_events_do_not_count():
    # Joins, leaves, pins and title changes happen *to* a chat, not *in* it.
    for ct in (types.ContentType.NEW_CHAT_MEMBERS, types.ContentType.LEFT_CHAT_MEMBER,
               types.ContentType.PINNED_MESSAGE, types.ContentType.NEW_CHAT_TITLE):
        assert is_human(_Msg(ct, _User())) is False, ct


def test_message_without_a_sender_does_not_count():
    assert is_human(_Msg(types.ContentType.TEXT, None)) is False


def test_stickers_and_reactions_style_content_count():
    # Low-effort content is still a human being present; the *threshold* is what
    # keeps a lone sticker from marking a truck as in service, not this filter.
    assert is_human(_Msg(types.ContentType.STICKER, _User())) is True
