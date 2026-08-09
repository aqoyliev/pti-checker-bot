"""Read-only Telegram *user* client, used for the one thing the Bot API cannot
do: list the members of a group.

The Bot API exposes get_chat_member (needs a user_id you already have),
get_chat_member_count and getChatAdministrators -- there is no "list members".
Drivers are rarely admins, so onboarding needs a real roster, and only a user
account can produce one.

Strictly read-only: this never sends, joins, leaves or edits anything. It is
lazily connected on first use and shared afterwards, so a bot that never
onboards a group never opens the session at all.

Configuration (all optional -- absent config simply disables the feature):
  TELEGRAM_API_ID / TELEGRAM_API_HASH   the app credentials
  TELEGRAM_SESSION                      a Telethon StringSession
  TELEGRAM_SESSION_FILE                 path to a .session file (local dev)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from data.config import (
    TELEGRAM_API_HASH,
    TELEGRAM_API_ID,
    TELEGRAM_SESSION,
    TELEGRAM_SESSION_FILE,
)

_client = None
_lock = asyncio.Lock()
_cache_warmed = False


@dataclass(frozen=True)
class Member:
    user_id: int
    name: str
    username: str | None
    is_bot: bool

    @property
    def label(self) -> str:
        return self.name or (f"@{self.username}" if self.username else str(self.user_id))


def is_configured() -> bool:
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH
                and (TELEGRAM_SESSION or TELEGRAM_SESSION_FILE))


async def _get_client():
    """Connect once, reuse. Returns None when unconfigured or unusable."""
    global _client
    if not is_configured():
        return None
    if _client is not None:
        return _client

    async with _lock:
        if _client is not None:
            return _client
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession

            session = (StringSession(TELEGRAM_SESSION) if TELEGRAM_SESSION
                       else TELEGRAM_SESSION_FILE)
            client = TelegramClient(session, int(TELEGRAM_API_ID), TELEGRAM_API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                logging.error("userbot session is not authorized — member lookup disabled")
                await client.disconnect()
                return None
            _client = client
            logging.info("userbot session connected (member lookup available)")
        except Exception:
            logging.exception("could not start the userbot session")
            return None
    return _client


async def _drop_client():
    """Forget the cached client so the next call builds a fresh one.

    The client is cached for the process's lifetime, so a session that dies
    mid-flight (revoked key, dropped socket) leaves every later lookup failing
    with ConnectionError until the bot is redeployed -- which is exactly what
    happened after an auth key was revoked on 2026-08-09. Dropping it on a
    connection error makes recovery automatic once the session is valid again.
    """
    global _client, _cache_warmed
    stale, _client, _cache_warmed = _client, None, False
    if stale is not None:
        try:
            await stale.disconnect()
        except Exception:
            pass


async def _resolve(client, group_id: int):
    """Get the entity for a raw chat id, warming the dialog cache if needed.

    Telethon cannot address a chat by bare id alone -- it needs the access hash,
    which it only learns by seeing the chat, usually via the dialog list. On a
    fresh session every get_entity(-100...) therefore raises ValueError, which
    is how member lookup came back empty in production while the session itself
    was perfectly healthy. Walking the dialogs once fills the cache for good.
    """
    global _cache_warmed
    try:
        return await client.get_entity(group_id)
    except ValueError:
        if _cache_warmed:
            raise
        logging.info("warming userbot entity cache (first unresolved chat)")
        async for _ in client.iter_dialogs():
            pass
        _cache_warmed = True
        return await client.get_entity(group_id)


async def list_members(group_id: int, limit: int = 200) -> list[Member]:
    """Members of `group_id`, or [] if unavailable.

    Returns [] rather than raising: onboarding must still work (degraded) when
    the account is not in that group, the session is missing, or Telegram
    refuses the participant list.
    """
    client = await _get_client()
    if client is None:
        return []
    try:
        entity = await _resolve(client, group_id)
        # A basic group upgraded to a supergroup leaves a tombstone whose member
        # list is forbidden; the real chat is what migrated_to points at.
        if getattr(entity, "migrated_to", None) is not None:
            entity = await client.get_entity(entity.migrated_to)
        participants = await client.get_participants(entity, limit=limit)
    except Exception as e:
        logging.warning("userbot could not list members of %s: %s", group_id, type(e).__name__)
        if isinstance(e, (ConnectionError, OSError)):
            await _drop_client()
        return []

    out = []
    for p in participants:
        name = " ".join(filter(None, [p.first_name, p.last_name])).strip()
        out.append(Member(user_id=p.id, name=name, username=p.username,
                          is_bot=bool(getattr(p, "bot", False))))
    return out


async def get_description(group_id: int) -> str:
    """The group's About text, or "" when unavailable."""
    client = await _get_client()
    if client is None:
        return ""
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest
        from telethon.tl.types import Channel

        entity = await _resolve(client, group_id)
        if isinstance(entity, Channel):
            full = await client(GetFullChannelRequest(entity))
        else:
            full = await client(GetFullChatRequest(entity.id))
        return (full.full_chat.about or "").strip()
    except Exception as e:
        logging.warning("userbot could not read description of %s: %s",
                        group_id, type(e).__name__)
        if isinstance(e, (ConnectionError, OSError)):
            await _drop_client()
        return ""


async def close():
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        finally:
            _client = None
