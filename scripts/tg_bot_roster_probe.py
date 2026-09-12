"""Probe: can the *bot* list a group's members over MTProto?

    railway run py -3.11 scripts/tg_bot_roster_probe.py -1001234567890
    railway run py -3.11 scripts/tg_bot_roster_probe.py -1001234567890 -1009876543210

`utils/userbot.py` exists because the **HTTP Bot API** has no "list members"
call -- only getChatAdministrators, getChatMember (needs a user_id you already
have) and getChatMemberCount. That is true and unchanged.

MTProto is a different layer, and there a bot that is an *administrator* of a
supergroup is documented as being able to call channels.getParticipants. If
that holds for this fleet's groups, the roster half of onboarding could run on
the bot token instead of a user session -- which would retire the whole
one-session-per-host problem (AuthKeyDuplicatedError, 2026-08-09) for member
lookup. Phone lookup could not move: contacts.importContacts is closed to bots,
so `/whois` keeps needing a user account either way.

This script answers only "does it work, and is the list complete" -- it writes
nothing, anywhere, and is not wired into the bot. Decide from its output.

Measured 2026-09-12, six groups across two fleets, **none of them with the bot
as an administrator**, every one fully listed:

    fleet      type         members  result
    Cross USA  supergroup         3  OK
    DMW        basic group       36  OK
    DMW        supergroup        33  OK
    DMW        supergroup        30  OK
    DMW        basic group       36  OK
    DMW        basic group       40  OK

So admin rights are not required, and both chat shapes answer. The one thing
this does not prove is behaviour on a group far larger than the fleet's:
channels.getParticipants pages in 200s and is capped server-side for
non-admins, which a 30-40 member group never reaches. That is why the
listed-vs-total check stays in -- it is what would catch the cap if a group
ever grew into it.

Two deliberate choices, both load-bearing:

- **receive_updates=False.** Telethon otherwise opens an update loop, and this
  bot is polling in production. A probe must not race the live bot for its
  updates.
- **MemorySession.** Nothing is written to disk, so there is no new session
  file to collide with `~/.pti-tg/*` or with the Railway session.

Still, this logs the bot into a second client while the deployed one is
running. Run it against one group you don't mind disturbing before you run it
against the fleet.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.tl.types import (
    Channel,
    Chat,
    InputPeerChannel,
    InputPeerChat,
    User,
)

SAMPLE = 5


def _use_utf8_stdout() -> None:
    """Windows consoles default to cp1252, and driver names carry emoji.

    A member displayed as a globe emoji killed a run mid-group on 2026-09-12:
    the roster had already arrived complete (30 of 30), but printing the sample
    raised UnicodeEncodeError, the per-chat handler caught it, and a group that
    worked was reported FAILED. What the console can encode must never decide a
    verdict about what Telegram returned.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 -- an odd stream keeps its default
            pass


def _env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"{name} is not set — run this under `railway run`.")
    return v


def _target(raw: str):
    """A chat id as the fleet writes it, or a @username."""
    try:
        return int(raw)
    except ValueError:
        return raw


def _candidates(target):
    """The peers worth trying for one id, most likely first.

    A bot has no dialog list to learn access hashes from, so each shape is
    built directly instead of looked up -- bots may address a channel with
    access_hash=0. The two shapes are not interchangeable and fail in
    different ways: -100<n> is a supergroup, a bare -<n> a basic group, and
    handing a supergroup to the basic-group call returns ChatIdInvalidError.
    A dropped -100 prefix is an easy transcription slip, so a bare negative id
    is tried both ways before the chat is called unreachable.
    """
    if not isinstance(target, int):
        return [("as given", target)]
    if target <= -1000000000000:
        return [("supergroup", InputPeerChannel(-target - 1000000000000, 0))]
    if target < 0:
        return [("basic group", InputPeerChat(-target)),
                ("supergroup", InputPeerChannel(-target, 0))]
    return [("as given", target)]


def _kind(entity) -> str:
    if isinstance(entity, Channel):
        return "supergroup" if entity.megagroup else "channel"
    if isinstance(entity, Chat):
        return "basic group"
    return type(entity).__name__


async def _about(client, peer, entity) -> str | None:
    """The group's About text -- the *other* thing utils/userbot.py is used for.

    parse_driver_names and the whole auto-config path read it, so moving the
    roster to the bot token only retires TELEGRAM_SESSION if this moves too.
    """
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import GetFullChatRequest
    try:
        if isinstance(peer, InputPeerChat):
            full = await client(GetFullChatRequest(peer.chat_id))
        else:
            full = await client(GetFullChannelRequest(entity))
        return (full.full_chat.about or "").strip()
    except Exception:  # noqa: BLE001 -- a probe reports, never raises
        return None


async def _attempt(client, shape, peer) -> bool:
    """Read one chat through one id shape. True only when fully listed."""
    try:
        entity = await client.get_entity(peer)
        print(f"  title         : {getattr(entity, 'title', '(none)')}")
        print(f"  type          : {_kind(entity)}")
    except Exception:  # noqa: BLE001 -- the input peer is enough for a bot
        entity = peer
        print(f"  type          : {shape} (assumed; get_entity declined)")

    try:
        perms = await client.get_permissions(entity, await client.get_me())
        print(f"  bot is admin  : {'yes' if perms.is_admin else 'NO'}")
    except Exception as e:  # noqa: BLE001 -- a probe reports, never raises
        print(f"  bot is admin  : unknown ({type(e).__name__}: {e})")

    parts = await client.get_participants(entity)
    total = getattr(parts, "total", len(parts))
    humans = [p for p in parts if isinstance(p, User) and not p.bot]
    print(f"  participants  : {len(parts)} listed of {total} total "
          f"({len(humans)} non-bot)")
    if parts:
        shown = ", ".join(
            (p.first_name or p.username or str(p.id))
            + (" [bot]" if getattr(p, "bot", False) else "")
            for p in parts[:SAMPLE])
        # Belt and braces behind _use_utf8_stdout: if a console still refuses
        # a character, the sample is the one line worth losing, never the run.
        try:
            print(f"  sample        : {shown}{' …' if len(parts) > SAMPLE else ''}")
        except UnicodeEncodeError:
            print(f"  sample        : ({len(parts)} names, console cannot print them)")

    # The About text is never echoed here: it carries the drivers' phone
    # numbers, and this output gets pasted around. Its length and how many
    # long digit-runs it holds say whether the right text arrived.
    about = await _about(client, peer, entity)
    if about is None:
        print("  about text    : FAILED — /fixnames and auto-config still need "
              "the user session")
    else:
        # Space but not newline: \s would swallow the line break between two
        # numbers and count the pair as one.
        phones = len(re.findall(r"\d[\d\-() .]{8,}\d", about))
        print(f"  about text    : {len(about)} chars, {phones} phone-shaped run(s)")

    # A short list against a large total is the failure that looks like
    # success: onboarding would show a picker with the drivers missing from it.
    if len(parts) < total:
        print(f"  VERDICT       : PARTIAL — {total - len(parts)} member(s) not returned")
        return False
    print("  VERDICT       : OK — the bot can list this group's roster")
    return True


async def probe(client, raw: str) -> bool:
    """Try each id shape. One chat's failure never stops the run."""
    print(f"\n{raw}")
    for shape, peer in _candidates(_target(raw)):
        try:
            return await _attempt(client, shape, peer)
        except Exception as e:  # noqa: BLE001
            print(f"  as {shape:<11}: {type(e).__name__}: {e}")
    print("  VERDICT       : FAILED — unreadable as any known id shape "
          "(wrong fleet's token, or the bot is not in this chat)")
    return False


async def run(chats: list[str]) -> None:
    client = TelegramClient(
        MemorySession(),
        int(_env("TELEGRAM_API_ID")),
        _env("TELEGRAM_API_HASH"),
        receive_updates=False,
    )
    await client.start(bot_token=_env("BOT_TOKEN"))
    try:
        me = await client.get_me()
        print(f"probing as @{me.username} (bot, MTProto, read-only)")
        ok = 0
        for raw in chats:
            ok += await probe(client, raw)
        print(f"\n{ok}/{len(chats)} group(s) fully listable by the bot.")
        if ok < len(chats):
            print("Any FAILED or PARTIAL line means the roster still needs "
                  "utils/userbot.py — a partial list is worse than none, since "
                  "onboarding would show a picker with the drivers missing.")
    finally:
        await client.disconnect()


def main() -> None:
    _use_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("chats", nargs="+",
                    help="group ids as stored in the groups table (-100…), or @usernames")
    asyncio.run(run(ap.parse_args().chats))


if __name__ == "__main__":
    main()
