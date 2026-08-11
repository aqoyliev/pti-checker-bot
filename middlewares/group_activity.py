"""Counts human messages per group, one daily bucket at a time.

This is the live half of ``utils/group_activity.py``: it answers "is anyone
still using this chat?" without a single Telegram API call, because every group
update already tells us who posted.

Two exclusions define "human":

- **bots don't count.** The bot's own PTI results and reminders are the loudest
  traffic in a dead group; counting them would make every group it nags look
  alive. An anonymous admin *is* a human, though — Telegram attributes their
  message to GroupAnonymousBot (``is_bot``) with ``sender_chat`` set, so that
  combination is kept.
- **service events don't count.** Joins, leaves, pins and title changes happen
  to a chat rather than in it.

Unlike a last-seen stamp, this is not throttled: the report thresholds on a
*count*, so every message has to be counted. That is one small upsert per human
group message, which is why the storage is a per-group-per-day counter rather
than a row per message.
"""
import logging
from time import monotonic

from aiogram import types
from aiogram.dispatcher.middlewares import BaseMiddleware

from utils.db import bump_group_message_count

_GROUP_TYPES = (types.ChatType.GROUP, types.ChatType.SUPERGROUP)

# Content-bearing message types — i.e. someone actually said something. An
# allowlist rather than a service-message denylist: Telegram keeps adding
# service events (forum topics, video chats, boosts), and a missed new *content*
# type only costs a slightly low count, while a missed new *service* type would
# quietly resurrect dead groups.
_HUMAN_CONTENT = frozenset({
    types.ContentType.TEXT, types.ContentType.PHOTO, types.ContentType.VIDEO,
    types.ContentType.VIDEO_NOTE, types.ContentType.DOCUMENT, types.ContentType.AUDIO,
    types.ContentType.VOICE, types.ContentType.ANIMATION, types.ContentType.STICKER,
    types.ContentType.LOCATION, types.ContentType.VENUE, types.ContentType.CONTACT,
    types.ContentType.POLL, types.ContentType.DICE, types.ContentType.GAME,
})


def is_human(message: types.Message) -> bool:
    user = message.from_user
    if user is None:
        return False
    # Anonymous admins are posted "as the group": from_user is GroupAnonymousBot
    # but sender_chat identifies the chat itself. Real bots have no sender_chat.
    if user.is_bot and message.sender_chat is None:
        return False
    return message.content_type in _HUMAN_CONTENT


# A failing write must still be *visible*. Counting failures look identical to
# silence — every count stays absent, and the quiet report confidently calls the
# whole fleet dead — so a swallowed error here would be indistinguishable from a
# healthy answer. Logged at most once every few minutes so a sustained outage
# reports the problem instead of flooding the log with one line per message.
_ERROR_LOG_INTERVAL = 300  # seconds
_last_error_log = float("-inf")


class GroupActivityMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        chat = message.chat
        if chat.type not in _GROUP_TYPES or not is_human(message):
            return
        try:
            await bump_group_message_count(chat.id)
        except Exception:
            # Activity is derived reporting data — never fail a message (or a
            # PTI submission) over it.
            global _last_error_log
            now = monotonic()
            if now - _last_error_log >= _ERROR_LOG_INTERVAL:
                _last_error_log = now
                logging.warning("group activity counting is failing (group %s) — "
                                "the quiet report will read every group as silent",
                                chat.id, exc_info=True)
