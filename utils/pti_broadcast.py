"""The Monday / Wednesday / Friday morning PTI call, posted into every group.

A picture of what to check, with the safety team's message under it as the
caption, at 07:00 Eastern on those three days (2026-10-14, at the fleet's
instruction). It is a call to film the walkaround *before the truck moves*, so
it goes out at the start of the driver's day rather than whenever a loop
happens to wake.

This is the one fixed-cadence message the bot sends, and it is kept apart from
the reminder engine on purpose:

* **It is not a reminder.** It never stamps ``groups.last_reminder_at``. That
  stamp is the one-per-24-hours cap on the overdue escalation, and stamping it
  three mornings a week would hold the escalation off for most of the week --
  the reminder that is driven by an actual missing PTI would lose to the one
  driven by the calendar.
* **It never deactivates a group.** A failed post is recorded for
  ``utils/group_health`` (the "bot is muted" alert) and logged; the unreachable
  strikes stay the reminder sender's business, same as the panel's broadcast.

Who gets it: active groups that are **set up** -- a unit and at least one
registered driver, which is what /check demands -- minus the ones an admin
switched notifications off for, minus the ones the bot is known to be muted
in. An unconfigured group is left out because a driver who answers the call
there is refused by /check. Quiet groups are *not* skipped -- a group nobody
has posted in is exactly where a PTI is missing.

The picture is **uploaded once**: the first group gets it as a fresh photo, and
every other group gets a copy of that very message (``copyMessage`` by its
``message_id``), so nothing is re-sent over the wire. A copy can fail for a
reason that is the source's and not the target's -- the message was deleted, or
that group protects its content -- so a failed copy falls back to posting the
photo by ``file_id``, and if *that* lands, the new message becomes the source.

When a run has posted anything, the super-admins get one line saying how many
groups it reached and how many failed.

Each group is **claimed before it is posted to** (``broadcast_sent``, keyed on
the day), so a restart in the middle of a run -- a deploy at 07:00 -- resumes
with the groups that were not reached yet and never posts twice. A bot that was
down at 07:00 still sends until 09:00; after that the morning's call is missed
rather than arriving mid-shift.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.utils.exceptions import MigrateToChat

from data.config import PTI_BROADCAST_ENABLED, PTI_BROADCAST_TZ
from loader import bot
from utils.admins import notify_super_admins
from utils.db import (
    claim_broadcast, get_broadcast_targets, migrate_group_id,
    prune_broadcast_sent,
)
from utils.group_health import note_send_failure, note_send_ok

KIND = "pti_morning"
DAYS = frozenset({0, 2, 4})  # Monday, Wednesday, Friday
SEND_AT = time(7, 0)
# Long enough to cover a deploy or a restart at 07:00, short enough that the
# call to inspect before moving does not land after the truck has left.
GRACE = timedelta(hours=2)
TICK_SECONDS = 60
# Between posts. ~10 a second, well under the Bot API's global limit, and the
# whole fleet still goes out inside half a minute.
PAUSE_SECONDS = 0.1
KEEP_DAYS = 30

IMAGE = Path(__file__).resolve().parent.parent / "assets" / "pti_broadcast.jpg"

# The safety team's text, as they wrote it. Escaped once here, because every
# message is HTML and "safe & DOT-ready" carries a bare ampersand.
TEXT = (
    "Good morning.\n\n"
    "PTI CHECK: Hello dear drivers — please record and send a 7‑minute "
    "walk‑around video (lights + tires + fluids + hoses) before you move.\n\n"
    "▫️ Keep engine idling.\n\n"
    "Thanks for keeping the fleet safe & DOT‑ready. 👍\n\n"
    "Best regards,\n"
    "Safety team"
)
CAPTION = escape(TEXT, quote=False)

# The first message this process posted -- (chat_id, message_id) -- which every
# other group gets a copy of, and the photo's file_id, the fallback when a copy
# cannot be made. In memory only: a restart uploads the picture once more.
_source: tuple[int, int] | None = None
_photo_id: str | None = None
# The day the last run finished, so the rest of the two-hour window costs
# nothing. In memory only: after a restart the claims in the database decide.
_done: date | None = None


def due_day(now: datetime, tz: ZoneInfo) -> date | None:
    """The local day whose broadcast is due at `now`, or None.

    `now` must be timezone-aware. Local time decides, so 07:00 follows daylight
    saving: 11:00 UTC in summer, 12:00 UTC in winter.
    """
    local = now.astimezone(tz)
    if local.weekday() not in DAYS:
        return None
    start = datetime.combine(local.date(), SEND_AT, tzinfo=tz)
    if start <= local < start + GRACE:
        return local.date()
    return None


async def _post(group_id: int) -> None:
    global _source, _photo_id
    if _source is not None:
        try:
            await bot.copy_message(group_id, _source[0], _source[1])
            return
        except MigrateToChat:
            raise  # the target moved; that is not the source's problem
        except Exception as e:
            # Either side can be the reason. Posting the photo settles which:
            # if it lands, the source was the problem and is replaced below.
            logging.info("copying the PTI call to %s failed (%s); posting the "
                         "photo instead", group_id, type(e).__name__)
    photo = _photo_id or types.InputFile(str(IMAGE))
    message = await bot.send_photo(group_id, photo, caption=CAPTION,
                                   parse_mode="HTML")
    if getattr(message, "photo", None):
        _photo_id = message.photo[-1].file_id
    _source = (group_id, message.message_id)


async def run_broadcast_if_due(now: datetime | None = None) -> int:
    """Post the morning call to every group not yet reached today.

    Returns how many groups it was posted to on this call.
    """
    global _done
    now = now or datetime.now(timezone.utc)
    day = due_day(now, ZoneInfo(PTI_BROADCAST_TZ))
    if day is None or day == _done:
        return 0

    await prune_broadcast_sent(KEEP_DAYS)
    sent = failed = 0
    migrated = False
    for group_id in await get_broadcast_targets():
        # Claimed first: a crash between the two costs this group its message,
        # never a second copy of it.
        if not await claim_broadcast(KIND, day, group_id):
            continue
        try:
            await _post(group_id)
            await note_send_ok(group_id)
            sent += 1
        except MigrateToChat as e:
            # The group became a supergroup under a new id. Follow it; the new
            # id has no claim yet, so the next tick posts there.
            await migrate_group_id(group_id, e.migrate_to_chat_id)
            migrated = True
        except Exception as e:
            failed += 1
            await note_send_failure(group_id, e)
            logging.warning("PTI broadcast to %s failed: %s",
                            group_id, type(e).__name__)
        await asyncio.sleep(PAUSE_SECONDS)

    # A group that moved to a new id still has to be reached, so a run that
    # followed one is not finished: the next tick claims and posts the new id.
    if not migrated:
        _done = day
    if sent or failed:
        logging.info("PTI broadcast for %s: posted to %s group(s), %s failed",
                     day, sent, failed)
        await notify_super_admins(
            f"📣 Morning PTI call ({day:%a %d %b}): posted to {sent} "
            f"group(s)" + (f", {failed} failed — see the logs." if failed else "."))
    return sent


async def pti_broadcast_loop():
    if not PTI_BROADCAST_ENABLED:
        logging.info("PTI morning broadcast is off (PTI_BROADCAST_ENABLED)")
        return
    if not IMAGE.is_file():
        logging.error("PTI morning broadcast is off: %s is missing", IMAGE)
        return
    while True:
        try:
            await run_broadcast_if_due()
        except Exception:
            logging.exception("PTI morning broadcast failed")
        await asyncio.sleep(TICK_SECONDS)
