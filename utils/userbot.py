"""Read-only Telegram client used for the one thing the Bot API cannot do:
list the members of a group.

The Bot API exposes get_chat_member (needs a user_id you already have),
get_chat_member_count and getChatAdministrators -- there is no "list members".
Drivers are rarely admins, so onboarding needs a real roster.

This runs over MTProto **as the bot itself** (Telethon, logged in with
`BOT_TOKEN`), not a separate user account. channels.getParticipants works for
a bot that is merely a member of the group -- admin rights are not required --
confirmed 2026-09-12 against six live fleet groups (both basic groups and
supergroups, none with the bot as admin), every one fully listed. That retires
the one-session-per-host problem entirely for this half of onboarding: a bot
token can be logged in from any number of places at once, unlike a user
session's single authorization key (see `utils/phone_lookup.py`, which still
needs one -- `contacts.importContacts` is closed to bots).

Two deliberate choices:
  - MemorySession. Nothing is written to disk and there is no session file to
    manage or collide with anything -- the bot token is already the credential.
  - receive_updates=False. Without it Telethon opens its own update loop, and
    this bot is polling for updates elsewhere in the process; a second listener
    must not race the live one for them.

Strictly read-only: this never sends, joins, leaves or edits anything. It is
lazily connected on first use and shared afterwards, so a bot that never
onboards a group never opens this client at all.

Configuration:
  TELEGRAM_API_ID / TELEGRAM_API_HASH   the app credentials (MTProto needs
                                         these even when logging in as a bot)
  BOT_TOKEN                             already required for the Bot API
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from data.config import BOT_TOKEN, TELEGRAM_API_HASH, TELEGRAM_API_ID

_client = None
_lock = asyncio.Lock()


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
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH)


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
            from telethon.sessions import MemorySession

            client = TelegramClient(
                MemorySession(), int(TELEGRAM_API_ID), TELEGRAM_API_HASH,
                receive_updates=False,
            )
            await client.start(bot_token=BOT_TOKEN)
            _client = client
            logging.info("bot MTProto client connected (member lookup available)")
        except Exception:
            logging.exception("could not start the bot MTProto client")
            return None
    return _client


async def _drop_client():
    """Forget the cached client so the next call builds a fresh one.

    The client is cached for the process's lifetime, so a dropped socket would
    otherwise leave every later lookup failing with ConnectionError until the
    bot is redeployed.
    """
    global _client
    stale, _client = _client, None
    if stale is not None:
        try:
            await stale.disconnect()
        except Exception:
            pass


def _peer(group_id: int):
    """The InputPeer for a raw chat id, built directly rather than looked up.

    A bot has no dialog list to learn access hashes from the way a user
    session does, but bots may address a chat they belong to with
    access_hash=0. -100<n> is a supergroup/channel, a bare negative id a
    basic group -- handing a supergroup id to the basic-group shape (or vice
    versa) raises, which _resolve lets propagate as a normal failure.
    """
    from telethon.tl.types import InputPeerChannel, InputPeerChat

    if group_id <= -1_000_000_000_000:
        return InputPeerChannel(-group_id - 1_000_000_000_000, 0)
    return InputPeerChat(-group_id)


async def _resolve(client, group_id: int):
    """Get the entity for `group_id`, following a migration tombstone once.

    A basic group upgraded to a supergroup leaves a tombstone whose member
    list is forbidden; the real chat is what migrated_to points at. In normal
    operation the caller already passes the *current* id (handlers/groups/
    registration.on_chat_migrated moves the DB row), so this only guards a
    missed migration.
    """
    from telethon.tl.types import InputPeerChannel

    entity = await client.get_entity(_peer(group_id))
    migrated = getattr(entity, "migrated_to", None)
    if migrated is not None:
        entity = await client.get_entity(
            InputPeerChannel(migrated.channel_id, migrated.access_hash))
    return entity


async def list_members(group_id: int, limit: int = 200) -> list[Member]:
    """Members of `group_id`, or [] if unavailable.

    Returns [] rather than raising: onboarding must still work (degraded) when
    the bot is not in that group, MTProto is unconfigured, or Telegram refuses
    the participant list.
    """
    client = await _get_client()
    if client is None:
        return []
    try:
        entity = await _resolve(client, group_id)
        participants = await client.get_participants(entity, limit=limit)
    except Exception as e:
        logging.warning("could not list members of %s: %s", group_id, type(e).__name__)
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
        logging.warning("could not read description of %s: %s",
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
