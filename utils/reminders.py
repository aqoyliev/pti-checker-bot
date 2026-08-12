"""PTI reminder engine (#8 twice-weekly nudge, #9 overdue escalation).

Deliberately separate from utils/enforcement.py: this NEVER restricts anyone, it
only posts reminders into the driver's group. The hourly scheduler calls
``run_reminder_pass``. Groups with ``notifications_disabled`` (toggled from the
admin panel, #10) are excluded at the query level and never reminded.

The decision logic is split into pure functions (``decide_weekly`` /
``decide_overdue_action``) so it can be unit-tested without a DB or Telegram.
"""
from __future__ import annotations

import logging
from datetime import datetime
from html import escape

from aiogram.utils.exceptions import BotBlocked, BotKicked, ChatNotFound, MethodIsNotAvailable

from loader import bot
from utils.email_alerts import send_overdue_alert
from utils.db import (
    get_drivers,
    get_groups_for_reminders,
    get_last_pti_for_group,
    mark_escalation_reminded,
    mark_group_inactive,
    mark_overdue_reminded,
    mark_weekly_reminder,
    mark_unreachable,
    clear_unreachable,
    UNREACHABLE_LIMIT,
    reset_group_reminders,
)
from utils.reminder_logic import OVERDUE_DAYS, decide_overdue_action, decide_weekly

_UNREACHABLE = (ChatNotFound, BotKicked, BotBlocked, MethodIsNotAvailable)


def _driver_names_plain(drivers: list[dict]) -> str:
    names = [d["name"] for d in drivers if d.get("name")]
    return ", ".join(names) if names else "Driver"


def _driver_names(drivers: list[dict]) -> str:
    """The drivers as tappable @-mentions, so a reminder actually notifies them.

    ``tg://user?id=`` is the only mention form that works without a username --
    most drivers don't have one, and a plain name is just text the app never
    pings. Telegram resolves the id against the group's own members, so this
    only notifies people already in the chat.

    A driver row with no user_id (or no name) falls back to escaped text rather
    than being dropped: a reminder naming everyone un-tappably is still a
    reminder, one that silently omits a driver is not.
    """
    parts = []
    for d in drivers:
        name = d.get("name")
        if not name:
            continue
        uid = d.get("user_id")
        parts.append(f'<a href="tg://user?id={int(uid)}">{escape(name)}</a>'
                     if uid else escape(name))
    return ", ".join(parts) if parts else "Driver"


# Every reminder spells out the submission step: send the video, then reply
# <code>/check</code> to it. A registered driver's standalone video is picked up
# automatically, but albums and photos always need the explicit reply, so the
# instruction is the one that works in every case.
_HOW = "Reply <code>/check</code> to your PTI video."


def _weekly_text(drivers: list[dict]) -> str:
    return (
        f"⏰ <b>PTI reminder</b> — {_driver_names(drivers)}, please send your "
        f"pre-trip inspection video. {_HOW}\n"
        "Drivers should submit a PTI at least twice a week."
    )


def _overdue_text(drivers: list[dict]) -> str:
    return (
        f"⚠️ <b>No PTI in {OVERDUE_DAYS} days.</b> {_driver_names(drivers)}, please send "
        f"your PTI video today. {_HOW}"
    )


def _escalation_text(drivers: list[dict]) -> str:
    return (
        f"🚨 {_driver_names(drivers)}, your PTI is still overdue. Please send a PTI video "
        f"now. {_HOW}\nThis reminder repeats every 12 hours until you do."
    )


async def _send(group_id: int, text: str) -> bool:
    """Send a reminder; return False if the group looks unreachable.

    Deactivation needs UNREACHABLE_LIMIT *consecutive* failures. A single one
    proves nothing: the bot talks to a local Bot API server whose chat state is
    lost on restart, after which it answers "chat not found" for groups the bot
    is still a member of. Treating that as terminal silently deactivated a
    large batch of healthy groups.
    """
    try:
        await bot.send_message(group_id, text, parse_mode="HTML")
        await clear_unreachable(group_id)
        return True
    except _UNREACHABLE as e:
        strikes = await mark_unreachable(group_id)
        if strikes >= UNREACHABLE_LIMIT:
            logging.warning(
                "Group %s unreachable %s times in a row (%s) — deactivating",
                group_id, strikes, type(e).__name__,
            )
            await mark_group_inactive(group_id)
        else:
            logging.warning(
                "Group %s unreachable (%s), strike %s/%s — keeping it active",
                group_id, type(e).__name__, strikes, UNREACHABLE_LIMIT,
            )
        return False
    except Exception:
        logging.exception("Failed to send reminder to group %s", group_id)
        return True  # transient; keep the group active and retry next pass


async def _process_group(group: dict, now: datetime) -> None:
    group_id = group["group_id"]
    drivers = await get_drivers(group_id)
    if not drivers:
        return  # nobody to remind

    # #8 — twice-weekly nudge.
    if decide_weekly(now, group.get("last_weekly_reminder_on")):
        if not await _send(group_id, _weekly_text(drivers)):
            return
        await mark_weekly_reminder(group_id, now.date())

    # #9 — overdue escalation. Reference = last PTI, else group creation time.
    last = await get_last_pti_for_group(group_id)
    reference = (last and last["submitted_at"]) or group.get("created_at") or now
    action = decide_overdue_action(
        now, reference, group.get("overdue_reminded_at"), group.get("last_escalation_at")
    )

    if action == "reset":
        await reset_group_reminders(group_id)
    elif action == "overdue_first":
        if await _send(group_id, _overdue_text(drivers)):
            await mark_overdue_reminded(group_id, now)
    elif action == "escalation":
        if await _send(group_id, _escalation_text(drivers)):
            await mark_escalation_reminded(group_id, now)
            # Email the global alert address on the same 12-hour cadence as the
            # group nag (no-op unless SMTP + recipient are configured).
            await send_overdue_alert(
                group.get("unit_number"),
                _driver_names_plain(drivers),
                (now - reference).days,
            )


async def run_reminder_pass() -> None:
    """One sweep over all reminder-eligible groups. Called hourly by the scheduler."""
    now = datetime.utcnow()
    for group in await get_groups_for_reminders():
        try:
            await _process_group(group, now)
        except Exception:
            logging.exception("Reminder pass failed for group %s", group.get("group_id"))
