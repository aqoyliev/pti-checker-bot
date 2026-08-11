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

A truck leaving the list also retires its group: any active group whose
``unit_number`` is missing from the new list is deactivated. That is a big,
fleet-wide write driven by one pasted message, and a truncated or mistyped paste
once flipped ``is_active`` FALSE across the fleet -- so the sweep is *previewed*
and only runs once the admin confirms. Nothing is written until then, not even
the list itself.

Two deliberate asymmetries:

- **Deactivate only.** A unit reappearing on a later list does not reactivate
  its group; that stays a manual panel decision, so the weekly paste can never
  undo a deactivation someone made for an unrelated reason.
- **A group with no unit is left alone.** The list is about trucks, and a group
  still waiting for onboarding has no unit to match -- reading that as "not in
  the list" would retire every group before it was ever configured.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from html import escape
from time import monotonic

from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.config import ADMINS
from loader import bot, dp
from utils.db import (
    active_units_updated_at,
    deactivate_groups,
    get_active_units,
    get_all_groups,
    get_group_message_counts,
    get_setting,
    normalize_unit,
    prune_group_message_days,
    replace_active_units,
    set_setting,
)
from utils.group_activity import GROUP_QUIET_DAYS, quiet_groups

_ADMIN_IDS = [int(a) for a in ADMINS if str(a).strip().isdigit()]

# A pending /units confirmation, per admin: {"units": [...], "casualties": [...]}.
# In memory on purpose — a prompt that doesn't survive a restart is the safe
# failure mode for a write this wide, and the admin just re-pastes the list.
_pending: dict[int, dict] = {}
# How long a preview stays answerable before it's treated as stale.
_PENDING_TTL = 600  # seconds
# Cap the previewed groups so a fleet-wide sweep can't exceed Telegram's message limit.
_PREVIEW_LIMIT = 30

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


def groups_to_deactivate(groups: list[dict], units: list[str]) -> list[dict]:
    """Active groups whose unit is missing from the new weekly list.

    Pure, so the sweep's blast radius can be asserted in tests without a DB.
    Units are compared through ``normalize_unit`` because group units were typed
    or imported ("<1304 >") while the list is parsed to bare digit runs.

    Skipped on purpose: groups already inactive (nothing to do) and groups with
    no unit at all -- an un-onboarded group isn't a retired truck.
    """
    wanted = {normalize_unit(u) for u in units}
    out: list[dict] = []
    for g in groups:
        if not g.get("is_active", True):
            continue
        unit = normalize_unit(g.get("unit_number"))
        if not unit:
            continue
        if unit not in wanted:
            out.append(g)
    return out


def _group_line(g: dict) -> str:
    unit = escape(normalize_unit(g.get("unit_number")) or "—")
    title = escape(g.get("title") or str(g["group_id"]))
    return f"• <b>{unit}</b> — {title}"


def _confirm_text(units: list[str], casualties: list[dict]) -> str:
    shown = casualties[:_PREVIEW_LIMIT]
    lines = [
        f"📋 <b>{len(units)} active units</b> ready to store.",
        "",
        f"⚠️ <b>{len(casualties)} group(s) would be deactivated</b> — their unit "
        f"is not on this list:",
        "",
        *[_group_line(g) for g in shown],
    ]
    if len(casualties) > len(shown):
        lines.append(f"…and {len(casualties) - len(shown)} more.")
    lines += ["", "Nothing has been saved yet. Confirm to store the list and "
                  "deactivate these groups."]
    return "\n".join(lines)


def _confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Confirm", callback_data="un:ok"),
        InlineKeyboardButton("✖️ Cancel", callback_data="un:no"),
    )
    return kb


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

    casualties = groups_to_deactivate(await get_all_groups(), units)
    if casualties:
        # Nothing is written yet — not the list, not the groups. Both land
        # together in _apply(), or neither does.
        _pending[message.from_user.id] = {
            "units": units,
            "casualties": casualties,
            "at": monotonic(),
        }
        await message.answer(_confirm_text(units, casualties), parse_mode="HTML",
                             reply_markup=_confirm_kb())
        return

    await message.answer(await _apply(units, []), parse_mode="HTML")


async def _apply(units: list[str], casualties: list[dict]) -> str:
    """Store the list and retire the groups that fell off it. Returns the reply."""
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

    if casualties:
        count = await deactivate_groups([g["group_id"] for g in casualties])
        lines.append(f"\n💤 <b>{count} group(s) deactivated</b> — unit no longer "
                     f"on the list.")
        logging.info("units sweep deactivated %s groups: %s", count,
                     [g["group_id"] for g in casualties])
    return "\n".join(lines)


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("un:"))
async def units_confirm_callback(query: types.CallbackQuery):
    uid = query.from_user.id
    if uid not in _ADMIN_IDS:
        await query.answer("Not allowed.", show_alert=True)
        return

    state = _pending.pop(uid, None)
    if state is None or monotonic() - state["at"] > _PENDING_TTL:
        # Re-pasting the list is cheap; acting on a stale preview is not — the
        # fleet may have changed since it was drawn.
        await query.message.edit_text("That confirmation expired — send /units again.")
        await query.answer()
        return

    if query.data == "un:no":
        await query.message.edit_text("✖️ Cancelled — nothing was saved.")
        await query.answer()
        return

    text = await _apply(state["units"], state["casualties"])
    await query.message.edit_text(text, parse_mode="HTML")
    await query.answer()


async def _quiet_report() -> str:
    """The quiet-groups list, as HTML. Safe to call even with no traffic data."""
    groups = await get_all_groups()
    counts = await get_group_message_counts(GROUP_QUIET_DAYS)
    quiet = quiet_groups(groups, counts)
    if not quiet:
        return (f"🟢 <b>No quiet groups</b> — every active group has been used in "
                f"the last {GROUP_QUIET_DAYS} days.")

    lines = [f"🌙 <b>{len(quiet)} quiet group(s)</b> — little or no human traffic "
             f"in the last {GROUP_QUIET_DAYS} days:", ""]
    lines += [_group_line(g) for g in quiet[:_PREVIEW_LIMIT]]
    if len(quiet) > _PREVIEW_LIMIT:
        lines.append(f"…and {len(quiet) - _PREVIEW_LIMIT} more.")
    return "\n".join(lines)


@dp.message_handler(commands=["quiet"], chat_type=types.ChatType.PRIVATE)
async def cmd_quiet(message: types.Message):
    if message.from_user.id not in _ADMIN_IDS:
        return
    await message.answer(await _quiet_report(), parse_mode="HTML")


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

    # The quiet list rides along with the ask: which trucks went silent is
    # exactly the question being answered when deciding what goes on the new
    # list. A failure to build it must not cost the ask itself.
    try:
        body = f"{_ASK_TEXT}\n\n{await _quiet_report()}"
    except Exception:
        logging.exception("could not build the quiet-groups report")
        body = _ASK_TEXT

    sent = False
    for admin_id in _ADMIN_IDS:
        try:
            await bot.send_message(admin_id, body, parse_mode="HTML")
            sent = True
        except Exception:
            logging.exception("could not ask admin %s for the units list", admin_id)

    if sent:
        await set_setting(_LAST_ASKED_KEY, now.isoformat())
    return sent


async def units_refresh_loop():
    """Background loop: nudge the admins when the units list goes stale, and
    keep the message-count buckets from growing without bound."""
    import asyncio

    while True:
        try:
            await ask_for_units_if_stale()
        except Exception:
            logging.exception("units refresh loop failed")
        try:
            await prune_group_message_days()
        except Exception:
            logging.exception("pruning group message buckets failed")
        await asyncio.sleep(CHECK_INTERVAL.total_seconds())
