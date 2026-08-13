"""/fixnames — put the fleet's driver names on groups configured before it.

Onboarding now stores the name from a group's About text (utils/driver_names),
but every group configured before that is filed under whatever its drivers call
themselves on Telegram. This is the backfill, and it is deliberately the cheap
half of what onboarding does:

  * it re-reads each active group's About text and pairs the names it finds
    against the drivers already registered, by their words -- no phone lookup.
    Contact import is the most rate-limited thing a user account can do, and
    spending the lookup account fleet-wide to fix a display name would put
    member lookup (which onboarding depends on) at risk for a cosmetic win;
  * a pairing that isn't proven is reported, not guessed. The unproven ones are
    a per-group `/onboard <group_id>`, where the numbers *are* resolved.

Preview then confirm, like /units: one command that renames drivers fleet-wide
is worth showing before it writes.
"""
from __future__ import annotations

import asyncio
import logging
from html import escape
from time import monotonic

from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.config import ADMINS
from loader import dp
from utils import userbot
from utils.db import get_all_drivers_by_group, get_all_groups, set_driver_names
from utils.driver_names import match_names_to_drivers, parse_driver_names

_ADMIN_IDS = [int(a) for a in ADMINS if str(a).strip().isdigit()]

# A pending confirmation, per admin. In memory on purpose: a prompt that doesn't
# survive a restart is the safe failure mode for a write this wide, and the scan
# is repeatable.
_pending: dict[int, dict] = {}
_PENDING_TTL = 900  # seconds; the scan itself takes minutes
_PREVIEW_LIMIT = 25
# Between About-text reads. The same courtesy the title sweep pays: this walks
# the whole fleet on one user session.
_FETCH_DELAY = 0.2


def _label(group: dict) -> str:
    unit = group.get("unit_number") or "no unit"
    return f"<b>{escape(str(unit))}</b>"


async def scan_fleet() -> dict:
    """Work out every driver rename the About texts imply. Writes nothing.

    Returns {"changes": [...], "scanned": n, "no_names": n, "unplaced": n}.
    """
    groups = [g for g in await get_all_groups() if g.get("is_active", True)]
    by_group = await get_all_drivers_by_group()

    changes: list[dict] = []
    scanned = no_names = unplaced = 0
    for group in groups:
        gid = group["group_id"]
        drivers = by_group.get(gid) or []
        if not drivers:
            continue
        scanned += 1
        about = await userbot.get_description(gid)
        await asyncio.sleep(_FETCH_DELAY)
        names = parse_driver_names(about)
        if not names:
            no_names += 1
            continue
        placed, left = match_names_to_drivers(names, drivers)
        if left:
            unplaced += 1
        stored = {d["user_id"]: d.get("name") or "" for d in drivers}
        for user_id, name in placed.items():
            if stored.get(user_id) != name:
                changes.append({"group_id": gid, "user_id": user_id,
                                "old": stored.get(user_id, ""), "new": name,
                                "group": group})
    return {"changes": changes, "scanned": scanned, "no_names": no_names,
            "unplaced": unplaced}


def _preview(result: dict) -> str:
    changes = result["changes"]
    lines = [f"👤 <b>{len(changes)} driver name(s) would change</b> across "
             f"{result['scanned']} configured group(s).", ""]
    for c in changes[:_PREVIEW_LIMIT]:
        old = escape(c["old"] or str(c["user_id"]))
        lines.append(f"{_label(c['group'])}  {old} → <b>{escape(c['new'])}</b>")
    if len(changes) > _PREVIEW_LIMIT:
        lines.append(f"…and {len(changes) - _PREVIEW_LIMIT} more.")

    skipped = []
    if result["no_names"]:
        skipped.append(f"{result['no_names']} group(s) have no name line in their "
                       f"About text")
    if result["unplaced"]:
        skipped.append(f"{result['unplaced']} group(s) had a name I couldn't match "
                       f"to a registered driver — run <code>/onboard &lt;group_id&gt;</code> "
                       f"for those, it resolves the phone numbers instead")
    if skipped:
        lines += ["", "<i>Left alone: " + "; ".join(skipped) + ".</i>"]
    lines += ["", "Nothing has been saved yet."]
    return "\n".join(lines)


def _confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Confirm", callback_data="fn:ok"),
        InlineKeyboardButton("✖️ Cancel", callback_data="fn:no"),
    )
    return kb


@dp.message_handler(commands=["fixnames"], chat_type=types.ChatType.PRIVATE)
async def cmd_fixnames(message: types.Message):
    if message.from_user.id not in _ADMIN_IDS:
        return

    await message.answer("Reading every group's About text — this takes a "
                         "minute or two…")
    try:
        result = await scan_fleet()
    except Exception:
        logging.exception("/fixnames scan failed")
        await message.answer("The scan failed — check the logs.")
        return

    if not result["changes"]:
        await message.answer(
            f"Nothing to change: every driver in the {result['scanned']} "
            f"configured group(s) I could read is already stored under the name "
            f"in the About text (or the About text doesn't name them).")
        return

    _pending[message.from_user.id] = {"changes": result["changes"], "at": monotonic()}
    await message.answer(_preview(result), parse_mode="HTML",
                         reply_markup=_confirm_kb())


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("fn:"))
async def fixnames_confirm(query: types.CallbackQuery):
    uid = query.from_user.id
    if uid not in _ADMIN_IDS:
        await query.answer("Not allowed.", show_alert=True)
        return

    state = _pending.pop(uid, None)
    if state is None or monotonic() - state["at"] > _PENDING_TTL:
        # Re-scanning is cheap in risk terms; acting on a stale preview is not.
        await query.message.edit_text("That confirmation expired — send "
                                      "/fixnames again.")
        await query.answer()
        return

    if query.data == "fn:no":
        await query.message.edit_text("Cancelled. No names were changed.")
        await query.answer()
        return

    changes = state["changes"]
    changed = await set_driver_names(
        [(c["group_id"], c["user_id"], c["new"]) for c in changes])
    logging.info("/fixnames renamed %s driver(s) across %s group(s)",
                 changed, len({c["group_id"] for c in changes}))
    await query.message.edit_text(
        f"✅ Renamed {changed} driver(s) across "
        f"{len({c['group_id'] for c in changes})} group(s).")
    await query.answer("Saved")
