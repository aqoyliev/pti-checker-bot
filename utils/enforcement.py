from __future__ import annotations

import logging
from datetime import datetime, timedelta
from enum import Enum

from aiogram.types import ChatPermissions
from aiogram.utils.exceptions import (
    BotBlocked,
    BotKicked,
    CantRestrictChatOwner,
    ChatNotFound,
    MethodIsNotAvailable,
    NotEnoughRightsToRestrict,
)

from loader import bot
from data.config import ADMINS, ENFORCEMENT_ENABLED
from utils.db import (
    get_all_registered_groups, get_drivers,
    get_pti_count_this_week, get_last_pti,
    mark_group_inactive,
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

_UNREACHABLE_EXCEPTIONS = (ChatNotFound, BotKicked, BotBlocked, MethodIsNotAvailable)


class RestrictOutcome(Enum):
    """Result of a mute/unmute attempt, for the compliance loop to act on."""
    APPLIED = "applied"          # restriction set (or a transient miss we leave alone)
    OWNER = "owner"              # target is the chat creator — can't be restricted
    UNREACHABLE = "unreachable"  # group gone / bot lacks rights → caller deregisters


async def _deregister_group(group_id: int, reason: str):
    await mark_group_inactive(group_id)
    logging.warning(f"Group {group_id} marked inactive: {reason}")
    await notify_admins(
        f"⚠️ Group <code>{group_id}</code> is unreachable ({reason}). "
        f"Compliance checks are disabled until it is re-registered."
    )


async def _set_driver_restriction(group_id: int, user_id: int, permissions, action: str) -> RestrictOutcome:
    """Apply `permissions` to a driver. `action` ('mute'/'unmute') is for log text.

    Returns a RestrictOutcome: UNREACHABLE means the caller should deregister the
    group; OWNER means the target is the chat creator and can't be restricted;
    APPLIED covers success (and transient errors we deliberately leave alone).
    """
    try:
        await bot.restrict_chat_member(group_id, user_id, permissions=permissions)
        return RestrictOutcome.APPLIED
    except CantRestrictChatOwner:
        # Telegram never lets a bot restrict the chat creator, so enforcement
        # simply doesn't apply to the owner. Benign — recurs every cycle, so log
        # at debug to avoid spam.
        logging.debug(f"Cannot {action} chat owner {user_id} in group {group_id}; skipped")
        return RestrictOutcome.OWNER
    except NotEnoughRightsToRestrict:
        logging.warning(f"Bot lacks restrict rights in group {group_id} — can't {action} {user_id}")
        await notify_admins(
            f"⚠️ Bot lacks <b>Restrict Members</b> permission in group <code>{group_id}</code>.\n"
            f"Enforcement is disabled there. Grant admin rights or use /deregister to remove it."
        )
        return RestrictOutcome.UNREACHABLE
    except _UNREACHABLE_EXCEPTIONS as e:
        logging.warning(f"Group {group_id} unreachable while trying to {action} {user_id}: {type(e).__name__}")
        return RestrictOutcome.UNREACHABLE
    except Exception:
        logging.exception(f"Failed to {action} user {user_id} in group {group_id}")
        return RestrictOutcome.APPLIED


async def unmute_driver(group_id: int, user_id: int) -> RestrictOutcome:
    """Lift restrictions on a driver. See RestrictOutcome for the return contract."""
    return await _set_driver_restriction(group_id, user_id, _FULL_PERMISSIONS, "unmute")


async def mute_driver(group_id: int, user_id: int) -> RestrictOutcome:
    """Restrict a driver. See RestrictOutcome for the return contract."""
    return await _set_driver_restriction(group_id, user_id, _MUTED_PERMISSIONS, "mute")


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
    # The bot is not (and won't be) a group admin, so it can neither mute nor
    # unmute drivers. With enforcement off there is nothing to do — and probing
    # each group only triggers spurious "unreachable" deregistration alerts when
    # the restrict call fails for lack of rights. Bail out entirely.
    if not ENFORCEMENT_ENABLED:
        return

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

        for driver in drivers:
            user_id = driver["user_id"]
            name = driver["name"]

            compliant, reason = await check_driver_compliance(group_id, user_id)

            if compliant:
                if await unmute_driver(group_id, user_id) is RestrictOutcome.UNREACHABLE:
                    await _deregister_group(group_id, "unreachable during unmute")
                    break
                continue

            outcome = await mute_driver(group_id, user_id)
            if outcome is RestrictOutcome.UNREACHABLE:
                await _deregister_group(group_id, "unreachable during mute")
                break

            non_compliant.append(f"• {name} ({group_name}) — {reason}")

            # The chat owner can't actually be muted, so don't claim they were
            # restricted — just nudge them. Everyone else really is restricted.
            if outcome is RestrictOutcome.OWNER:
                reminder = f"⚠️ {name}, your PTI is overdue. Please submit one as soon as possible."
            else:
                reminder = (
                    f"⚠️ {name}, your PTI is overdue. "
                    f"You have been restricted until a PTI is submitted."
                )
            try:
                await bot.send_message(group_id, reminder)
            except _UNREACHABLE_EXCEPTIONS as e:
                await _deregister_group(group_id, type(e).__name__)
                break
            except Exception:
                logging.exception(f"Failed to send reminder in group {group_id}")

    if non_compliant:
        lines = ["🚨 <b>Non-compliant drivers:</b>\n"] + non_compliant
        await notify_admins("\n".join(lines))


async def handle_pti_passed(group_id: int, user_id: int, driver_name: str):
    """Call after any PTI is logged to re-evaluate compliance and unmute if eligible."""
    if not ENFORCEMENT_ENABLED:
        return
    compliant, _ = await check_driver_compliance(group_id, user_id)
    if compliant:
        await unmute_driver(group_id, user_id)
