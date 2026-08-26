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
    MigrateToChat,
    NotEnoughRightsToRestrict,
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

# RULE: the bot never restricts a driver. There is deliberately no "muted"
# permission set here and no mute_driver() — the only restriction call left in
# the module *lifts* restrictions. Non-compliance is answered with a reminder
# and an admin report, never by taking away someone's ability to post.
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
        # Only reachable from unmute_driver now. Nothing is broken by this — the
        # bot never restricts anyone — so it is a note, not an alert about
        # enforcement being off.
        logging.warning(f"Bot lacks restrict rights in group {group_id} — can't {action} {user_id}")
        await notify_admins(
            f"⚠️ Bot lacks <b>Restrict Members</b> permission in group <code>{group_id}</code>, "
            f"so it could not lift an old restriction on user <code>{user_id}</code>. "
            f"A group admin has to clear it by hand."
        )
        return RestrictOutcome.UNREACHABLE
    except _UNREACHABLE_EXCEPTIONS as e:
        logging.warning(f"Group {group_id} unreachable while trying to {action} {user_id}: {type(e).__name__}")
        return RestrictOutcome.UNREACHABLE
    except Exception:
        logging.exception(f"Failed to {action} user {user_id} in group {group_id}")
        return RestrictOutcome.APPLIED


async def unmute_driver(group_id: int, user_id: int) -> RestrictOutcome:
    """Lift restrictions on a driver. See RestrictOutcome for the return contract.

    This is the only restriction call the bot makes, and it only ever *removes*
    restrictions — there is no mute counterpart by design.
    """
    return await _set_driver_restriction(group_id, user_id, _FULL_PERMISSIONS, "unmute")


def is_gap_ok(last_pti: dict | None) -> bool:
    if not last_pti:
        return True
    last_dt = last_pti["submitted_at"]
    if isinstance(last_dt, str):
        last_dt = datetime.fromisoformat(last_dt)
    return datetime.utcnow() - last_dt.replace(tzinfo=None) >= timedelta(days=MIN_GAP_DAYS)


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

    The loop makes no restriction calls at all: a driver who is behind gets a
    message, not a muzzle. That also removes the old hazard where a group whose
    bot lacked Restrict Members rights looked "unreachable" and got deregistered
    on the strength of a failed mute.

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
            # Nobody is restricted, so the reminder never claims otherwise.
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


async def handle_pti_passed(group_id: int, user_id: int, driver_name: str):
    """Call after any PTI is logged to lift a restriction the driver may still carry.

    The bot no longer mutes anyone, so this exists only to clear restrictions
    left over from before that rule — it can never re-apply one.
    """
    if not ENFORCEMENT_ENABLED:
        return
    compliant, _ = await check_driver_compliance(group_id, user_id)
    if compliant:
        await unmute_driver(group_id, user_id)
