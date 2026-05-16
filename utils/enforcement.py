from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram.types import ChatPermissions
from aiogram.utils.exceptions import NotEnoughRightsToRestrict

from loader import bot
from data.config import ADMINS
from utils.db import (
    get_all_registered_groups, get_drivers,
    get_pti_count_this_week, get_last_pti,
)

REQUIRED_PER_WEEK = 2
MIN_GAP_DAYS = 3

_MUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_media_messages=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

_FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_media_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def unmute_driver(group_id: int, user_id: int):
    try:
        await bot.restrict_chat_member(group_id, user_id, permissions=_FULL_PERMISSIONS)
    except NotEnoughRightsToRestrict:
        logging.warning(f"Bot lacks restrict rights in group {group_id} — can't unmute {user_id}")
    except Exception:
        logging.exception(f"Failed to unmute user {user_id} in group {group_id}")


async def mute_driver(group_id: int, user_id: int):
    try:
        await bot.restrict_chat_member(group_id, user_id, permissions=_MUTED_PERMISSIONS)
    except NotEnoughRightsToRestrict:
        logging.warning(f"Bot lacks restrict rights in group {group_id} — can't mute {user_id}")
    except Exception:
        logging.exception(f"Failed to mute user {user_id} in group {group_id}")


def is_gap_ok(last_pti: dict | None) -> bool:
    if not last_pti:
        return True
    last_dt = last_pti["submitted_at"]
    if isinstance(last_dt, str):
        last_dt = datetime.fromisoformat(last_dt)
    return datetime.utcnow() - last_dt.replace(tzinfo=None) >= timedelta(days=MIN_GAP_DAYS)


async def check_driver_compliance(group_id: int, user_id: int) -> tuple[bool, str]:
    """Returns (is_compliant, reason)."""
    count = await get_pti_count_this_week(group_id, user_id)
    last = await get_last_pti(group_id, user_id)

    if count >= REQUIRED_PER_WEEK:
        return True, "weekly quota met"

    if last is None:
        return False, "no PTI submitted yet this week"

    last_dt = last["submitted_at"]
    gap = datetime.utcnow() - last_dt
    days_left = MIN_GAP_DAYS - gap.days
    if days_left > 0:
        return True, f"next PTI due in {days_left} day(s)"

    return False, f"only {count}/{REQUIRED_PER_WEEK} PTIs submitted this week"


async def notify_admins(text: str):
    for admin_id in ADMINS:
        try:
            await bot.send_message(int(admin_id), text, parse_mode="HTML")
        except Exception:
            logging.exception(f"Failed to notify admin {admin_id}")


async def run_compliance_check():
    groups = await get_all_registered_groups()
    non_compliant: list[str] = []

    for group in groups:
        group_id = group["group_id"]
        drivers = await get_drivers(group_id)

        for driver in drivers:
            user_id = driver["user_id"]
            name = driver["name"]
            compliant, reason = await check_driver_compliance(group_id, user_id)

            if compliant:
                await unmute_driver(group_id, user_id)
            else:
                await mute_driver(group_id, user_id)
                try:
                    chat = await bot.get_chat(group_id)
                    group_name = chat.title or str(group_id)
                except Exception:
                    group_name = str(group_id)
                non_compliant.append(f"• {name} ({group_name}) — {reason}")

                try:
                    await bot.send_message(
                        group_id,
                        f"⚠️ {name}, your PTI is overdue. "
                        f"You have been restricted until a PTI is submitted.",
                    )
                except Exception:
                    logging.exception(f"Failed to send reminder in group {group_id}")

    if non_compliant:
        lines = ["🚨 <b>Non-compliant drivers:</b>\n"] + non_compliant
        await notify_admins("\n".join(lines))


async def handle_pti_passed(group_id: int, user_id: int, driver_name: str):
    """Call after any PTI is logged to re-evaluate compliance and unmute if eligible."""
    compliant, _ = await check_driver_compliance(group_id, user_id)
    if compliant:
        await unmute_driver(group_id, user_id)
