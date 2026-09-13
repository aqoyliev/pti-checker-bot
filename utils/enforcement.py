"""The weekly-quota compliance check, and the rule it lives under.

**The bot never restricts, mutes or otherwise silences a driver.** There is no
``restrict_chat_member`` call anywhere in this module -- not to mute, and (as
of 2026-09-13) not to unmute either: the unmute path existed only to lift
restrictions applied before the rule, ran only behind ``ENFORCEMENT_ENABLED``,
and that flag has never been on in production, so it never ran at all. A
non-compliant driver gets a reminder in the group and a line in the admin
report, nothing else. A test asserts no mute helper exists.

``ENFORCEMENT_ENABLED`` gates only this module's hourly pass (the weekly
``REQUIRED_PER_WEEK`` quota reminder plus the admin summary). The 3-day
overdue escalation in ``utils/reminders.py`` runs regardless.
"""
from __future__ import annotations

import logging
from datetime import datetime

from aiogram.utils.exceptions import (
    BotBlocked,
    BotKicked,
    ChatNotFound,
    MethodIsNotAvailable,
    MigrateToChat,
)

from loader import bot
from data.config import ADMINS, ENFORCEMENT_ENABLED
from utils.db import (
    get_all_registered_groups, get_drivers,
    get_pti_count_this_week, get_last_pti,
    mark_group_inactive, mark_reminder_sent, migrate_group_id,
)
from utils.reminder_logic import may_remind

REQUIRED_PER_WEEK = 2
MIN_GAP_DAYS = 3

_UNREACHABLE_EXCEPTIONS = (ChatNotFound, BotKicked, BotBlocked, MethodIsNotAvailable)


async def _deregister_group(group_id: int, reason: str):
    await mark_group_inactive(group_id)
    logging.warning(f"Group {group_id} marked inactive: {reason}")
    await notify_admins(
        f"⚠️ Group <code>{group_id}</code> is unreachable ({reason}). "
        f"Compliance checks are disabled until it is re-registered."
    )


def compliance_verdict(
    week_count: int,
    last_pti_at: datetime | None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """(is_compliant, reason) from a driver's PTI count this week and their most
    recent submission (ever). Pure, so fleet-wide views can evaluate every driver
    from one get_weekly_pti_stats() batch instead of two queries per driver."""
    if now is None:
        now = datetime.utcnow()

    if week_count >= REQUIRED_PER_WEEK:
        return True, "weekly quota met"

    if last_pti_at is None:
        return False, "no PTI submitted yet this week"

    days_left = MIN_GAP_DAYS - (now - last_pti_at).days
    if days_left > 0:
        return True, f"next PTI due in {days_left} day(s)"

    return False, f"only {week_count}/{REQUIRED_PER_WEEK} PTIs submitted this week"


async def check_driver_compliance(group_id: int, user_id: int) -> tuple[bool, str]:
    """Returns (is_compliant, reason) for a single driver."""
    count = await get_pti_count_this_week(group_id, user_id)
    last = await get_last_pti(group_id, user_id)
    return compliance_verdict(count, last["submitted_at"] if last else None)


async def notify_admins(text: str):
    for admin_id in ADMINS:
        try:
            await bot.send_message(int(admin_id), text, parse_mode="HTML")
        except Exception:
            logging.exception(f"Failed to notify admin {admin_id}")


async def run_compliance_check():
    """Remind overdue drivers and report them to admins. Never restricts anyone.

    One reminder per unit per 24 hours. This runs hourly, so an overdue driver
    used to be told once an hour, every hour; and it shares the rule -- and the
    ``last_reminder_at`` stamp -- with the reminder engine, because two senders
    each keeping to their own budget is how a unit ends up messaged twice in a
    day. Both drivers of a truck are named in the one message for the same
    reason: the cap is per unit, not per driver.
    """
    # With reminders off there is nothing left for this loop to do.
    if not ENFORCEMENT_ENABLED:
        return

    now = datetime.utcnow()
    groups = await get_all_registered_groups()
    non_compliant: list[str] = []

    for group in groups:
        group_id = group["group_id"]

        try:
            chat = await bot.get_chat(group_id)
        except _UNREACHABLE_EXCEPTIONS as e:
            await _deregister_group(group_id, type(e).__name__)
            continue
        except Exception:
            logging.exception(f"Unable to fetch chat {group_id}; skipping this cycle")
            continue

        group_name = chat.title or str(group_id)
        drivers = await get_drivers(group_id)
        overdue_names: list[str] = []

        for driver in drivers:
            user_id = driver["user_id"]
            name = driver["name"]

            compliant, reason = await check_driver_compliance(group_id, user_id)

            if compliant:
                continue

            non_compliant.append(f"• {name} ({group_name}) — {reason}")
            overdue_names.append(name)

        # The admin report above lists every overdue driver every pass; the
        # group only hears about it once a day.
        if overdue_names and may_remind(now, group.get("last_reminder_at")):
            # Plain text: this send has no parse_mode, so no HTML markup here.
            reminder = (
                f"⚠️ {', '.join(overdue_names)}, your PTI is overdue. Please submit "
                f"one as soon as possible. Reply /check to your PTI video."
            )
            try:
                await bot.send_message(group_id, reminder)
                await mark_reminder_sent(group_id, now)
            except MigrateToChat as e:
                # Upgraded to a supergroup: the chat moved, it is not gone, so
                # this must not reach _deregister_group below. Nothing is sent
                # or stamped this pass -- mark_reminder_sent names the id that
                # has just moved -- and the next hourly pass finds the group
                # under its new id with its history intact.
                if await migrate_group_id(group_id, e.migrate_to_chat_id):
                    logging.info("Group %s was upgraded to supergroup %s; moved it",
                                 group_id, e.migrate_to_chat_id)
                continue
            except _UNREACHABLE_EXCEPTIONS as e:
                await _deregister_group(group_id, type(e).__name__)
                continue
            except Exception:
                logging.exception(f"Failed to send reminder in group {group_id}")

    if non_compliant:
        lines = ["🚨 <b>Non-compliant drivers:</b>\n"] + non_compliant
        await notify_admins("\n".join(lines))
