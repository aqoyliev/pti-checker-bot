"""Stamps ``groups.last_human_message_at`` from live group traffic.

This is the live half of ``utils/group_activity.py``: it answers "is anyone
still using this chat?" without a single Telegram API call, because every group
update already tells us who posted.

Two exclusions define "human", matching how ``scripts/tg_scan.py`` measures the
same thing offline:

- **bots don't count.** The bot's own PTI results and reminders are the loudest
  traffic in a dead group; counting them would make every group it nags look
  alive. An anonymous admin *is* a human, though — Telegram attributes their
  message to GroupAnonymousBot (``is_bot``) with ``sender_chat`` set, so that
  combination is kept.
- **service events don't count.** Joins, leaves, pins and title changes happen
  to a chat rather than in it.

Writes are throttled in memory, so a busy group costs one UPDATE every few
minutes instead of one per message; the stamp is compared against a multi-day
window, where minutes of lag are noise.
"""
from time import monotonic

from aiogram import types
from aiogram.dispatcher.middlewares import BaseMiddleware

from utils.db import touch_group_activity

_GROUP_TYPES = (types.ChatType.GROUP, types.ChatType.SUPERGROUP)

# Content-bearing message types — i.e. someone actually said something. An
# allowlist rather than a service-message denylist: Telegram keeps adding
# service events (forum topics, video chats, boosts), and a missed new *content*
# type only costs a slightly stale stamp, while a missed new *service* type
# would quietly resurrect dead groups.
_HUMAN_CONTENT = frozenset({
    types.ContentType.TEXT, types.ContentType.PHOTO, types.ContentType.VIDEO,
    types.ContentType.VIDEO_NOTE, types.ContentType.DOCUMENT, types.ContentType.AUDIO,
    types.ContentType.VOICE, types.ContentType.ANIMATION, types.ContentType.STICKER,
    types.ContentType.LOCATION, types.ContentType.VENUE, types.ContentType.CONTACT,
    types.ContentType.POLL, types.ContentType.DICE, types.ContentType.GAME,
})

_MIN_WRITE_INTERVAL = 300  # seconds between DB writes for the same group

_last_write: dict[int, float] = {}


def _is_human(message: types.Message) -> bool:
    user = message.from_user
    if user is None:
        return False
    # Anonymous admins are posted "as the group": from_user is GroupAnonymousBot
    # but sender_chat identifies the chat itself. Real bots have no sender_chat.
    if user.is_bot and message.sender_chat is None:
        return False
    return message.content_type in _HUMAN_CONTENT


class GroupActivityMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        chat = message.chat
        if chat.type not in _GROUP_TYPES or not _is_human(message):
            return
        now = monotonic()
        if now - _last_write.get(chat.id, float("-inf")) < _MIN_WRITE_INTERVAL:
            return
        _last_write[chat.id] = now
        try:
            await touch_group_activity(chat.id)
        except Exception:
            # Activity is derived reporting data — never fail a message (or a
            # PTI submission) over it. A group not yet in `groups` simply has
            # nothing to stamp.
            pass
