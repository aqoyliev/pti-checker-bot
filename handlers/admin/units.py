"""The fleet's active unit numbers, supplied by an admin once a week.

Onboarding guesses a unit from a group's title or description, but a title
outlives the truck: groups get renamed late, or not at all, so a guess can name
a unit that left the fleet months ago. Checking the guess against the current
list turns that into an honest "not found" instead of a confidently wrong unit
silently attached to a group.

The list is replaced wholesale rather than merged -- the weekly list *is* the
fleet, so a truck that drops off it has to disappear here too.

  /units                    show the current list's size and age
  /units 1332 1303 728453   replace the list (space, comma or newline separated)

An empty table disables the check rather than rejecting every unit, so the
feature is inert until the first list arrives.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from aiogram import types

from data.config import ADMINS
from loader import bot, dp
from utils.db import (
    active_units_updated_at,
    get_active_units,
    get_setting,
    replace_active_units,
    set_setting,
)

_ADMIN_IDS = [int(a) for a in ADMINS if str(a).strip().isdigit()]

# Kept apart from the list's own updated_at: asking must not look like the list
# was refreshed, or /units would report an age that is really the age of a nag.
_LAST_ASKED_KEY = "active_units_last_asked_at"

# How stale the list may get before the bot asks for a fresh one.
REFRESH_AFTER = timedelta(days=7)
# How often the loop wakes up to check that age.
CHECK_INTERVAL = timedelta(hours=6)

_ASK_TEXT = (
    "📋 <b>Weekly check — active units</b>\n\n"
    "Send me this week's active unit numbers so I can tell a live truck from a "
    "retired one when a new group shows up.\n\n"
    "Reply with:\n<code>/units 1332 1303 728453 ...</code>"
)


def parse_units(raw: str) -> list[str]:
    """Unit numbers out of a pasted blob: space, comma, newline or tab separated.

    Keeps leading zeros ("001", "0822" are real units), so these stay strings.
    Anything that isn't a run of digits is dropped rather than guessed at.
    """
    return [tok for tok in re.split(r"[^0-9]+", raw or "") if tok]


def _describe(count: int, updated: datetime | None) -> str:
    if not count:
        return ("No active-units list yet — every unit guess is accepted as-is.\n"
                "Send <code>/units 1332 1303 ...</code> to set one.")
    if updated is None:
        return f"{count} active units stored (age unknown)."
    days = (datetime.utcnow() - updated).days
    when = "today" if days == 0 else f"{days} day{'s' if days != 1 else ''} ago"
    return f"{count} active units stored, last updated {when}."


@dp.message_handler(commands=["units"], chat_type=types.ChatType.PRIVATE)
async def cmd_units(message: types.Message):
    if message.from_user.id not in _ADMIN_IDS:
        return

    raw = message.get_args() or ""
    units = parse_units(raw)

    if not units:
        current = await get_active_units()
        await message.answer(_describe(len(current), await active_units_updated_at()),
                             parse_mode="HTML")
        return

    added, removed = await replace_active_units(units)

    lines = [f"✅ <b>{len(units)} active units stored.</b>"]
    if added:
        lines.append(f"+{len(added)} new: {', '.join(sorted(added)[:20])}"
                     + (" …" if len(added) > 20 else ""))
    if removed:
        lines.append(f"−{len(removed)} removed: {', '.join(sorted(removed)[:20])}"
                     + (" …" if len(removed) > 20 else ""))
    if not added and not removed:
        lines.append("No change from the previous list.")
    await message.answer("\n".join(lines), parse_mode="HTML")


async def _last_asked_at() -> datetime | None:
    raw = await get_setting(_LAST_ASKED_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def ask_for_units_if_stale(now: datetime | None = None) -> bool:
    """DM the admins for a fresh list when the current one is a week old.

    Returns True if the ask went out. Two clocks, deliberately: the list's own
    age decides *whether* a refresh is due, and the last-asked time keeps an
    unanswered ask from repeating every cycle for a week.
    """
    now = now or datetime.utcnow()

    updated = await active_units_updated_at()
    if updated is not None and now - updated < REFRESH_AFTER:
        return False

    asked = await _last_asked_at()
    if asked is not None and now - asked < REFRESH_AFTER:
        return False

    sent = False
    for admin_id in _ADMIN_IDS:
        try:
            await bot.send_message(admin_id, _ASK_TEXT, parse_mode="HTML")
            sent = True
        except Exception:
            logging.exception("could not ask admin %s for the units list", admin_id)

    if sent:
        await set_setting(_LAST_ASKED_KEY, now.isoformat())
    return sent


async def units_refresh_loop():
    """Background loop: nudge the admins when the units list goes stale."""
    import asyncio

    while True:
        try:
            await ask_for_units_if_stale()
        except Exception:
            logging.exception("units refresh loop failed")
        await asyncio.sleep(CHECK_INTERVAL.total_seconds())
