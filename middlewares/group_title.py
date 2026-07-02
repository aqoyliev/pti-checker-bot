"""Keeps ``groups.title`` fresh from live traffic.

Every group message already carries the chat's current title, so syncing here
costs no Telegram API calls — it's what lets the web panel list all groups
without a get_chat fan-out. An in-memory last-seen map means the DB is touched
once per actual title change (and once per group after each restart), not per
message.
"""
from aiogram import types
from aiogram.dispatcher.middlewares import BaseMiddleware

from utils.db import set_group_title

_GROUP_TYPES = (types.ChatType.GROUP, types.ChatType.SUPERGROUP)

_last_seen: dict[int, str] = {}


class GroupTitleMiddleware(BaseMiddleware):
    async def on_pre_process_message(self, message: types.Message, data: dict):
        chat = message.chat
        if chat.type not in _GROUP_TYPES or not chat.title:
            return
        if _last_seen.get(chat.id) == chat.title:
            return
        _last_seen[chat.id] = chat.title
        try:
            await set_group_title(chat.id, chat.title)
        except Exception:
            # Title is cache-only convenience data — never fail a message on it
            # (e.g. a group the bot was added to but that isn't in `groups` yet).
            pass
