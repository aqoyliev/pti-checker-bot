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

The picture is **always sent by ``file_id``** (2026-10-14, at the fleet's
instruction). It is uploaded once, and the id Telegram gives back is kept in
``app_settings`` together with a hash of the picture, so a restart does not
upload it again and a new picture shipped in ``media/`` is uploaded once more
rather than answered with the old one. A stored id Telegram no longer accepts
is a file error, not the group's, and costs exactly one re-upload. (The first
version copied the first group's message by ``message_id`` instead; a copy
depends on a message somebody can delete and a group that may protect its
content, and the fleet chose the file_id.)

The run is reported to the **admin named "Safety Monitoring"**
(``PTI_BROADCAST_REPORT_ADMIN``) and to nobody else -- not the super-admin,
who asked not to get these (2026-10-14). The report names every group the call
did not reach and Telegram's own reason, since "1 failed -- see the logs" sent
someone to logs they cannot read.

Each group is **claimed before it is posted to** (``broadcast_sent``, keyed on
the day), so a restart in the middle of a run -- a deploy at 07:00 -- resumes
with the groups that were not reached yet and never posts twice. A bot that was
down at 07:00 still sends until 09:00; after that the morning's call is missed
rather than arriving mid-shift.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import date, datetime, time, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.utils.exceptions import BadRequest, MigrateToChat

from data.config import (
    PTI_BROADCAST_ENABLED, PTI_BROADCAST_REPORT_ADMIN, PTI_BROADCAST_TZ,
)
from loader import bot
from utils.admins import notify_admins_named
from utils.db import (
    claim_broadcast, get_broadcast_targets, get_setting, migrate_group_id,
    prune_broadcast_sent, set_setting,
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
# How many unreached groups the report names one by one; a message holds 4096
# characters, and past this many the count says enough.
REPORT_LIMIT = 40

# Not under assets/: .dockerignore keeps that out of the runtime image (it
# holds the logo sources), and the first deploy of this ran without its picture.
IMAGE = Path(__file__).resolve().parent.parent / "media" / "pti_broadcast.jpg"
FILE_ID_SETTING = "pti_broadcast_file_id"

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

# The uploaded photo's file_id, read from app_settings once per process.
_photo_id: str | None = None
_photo_loaded = False
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


def _image_digest() -> str:
    return hashlib.sha256(IMAGE.read_bytes()).hexdigest()[:16]


async def _stored_file_id() -> str | None:
    """The file_id saved for *this* picture, or None if there is none yet."""
    global _photo_id, _photo_loaded
    if not _photo_loaded:
        _photo_loaded = True
        digest, _, file_id = (await get_setting(FILE_ID_SETTING) or "").partition(" ")
        _photo_id = file_id if digest == _image_digest() and file_id else None
    return _photo_id


def _is_file_error(exc: BaseException) -> bool:
    """A stored file_id Telegram no longer accepts -- the file's fault, not the
    group's. Anything else (no rights, chat gone) is about the group."""
    return isinstance(exc, BadRequest) and "file" in str(exc).lower()


async def _post(group_id: int) -> None:
    global _photo_id
    file_id = await _stored_file_id()
    if file_id:
        try:
            await bot.send_photo(group_id, file_id, caption=CAPTION,
                                 parse_mode="HTML")
            return
        except BadRequest as e:
            if not _is_file_error(e):
                raise
            logging.warning("the stored PTI call file_id was refused (%s); "
                            "uploading the picture again", e)
            _photo_id = None
    message = await bot.send_photo(group_id, types.InputFile(str(IMAGE)),
                                   caption=CAPTION, parse_mode="HTML")
    if getattr(message, "photo", None):
        _photo_id = message.photo[-1].file_id
        await set_setting(FILE_ID_SETTING, f"{_image_digest()} {_photo_id}")


def _group_label(group: dict) -> str:
    """'Unit 2643 — <title>' as the fleet knows it, plus the id to act on."""
    unit = (group.get("unit_number") or "").strip()
    title = (group.get("title") or "").strip()
    if title and (not unit or unit in title):
        label = escape(title)
    else:
        label = f"Unit {escape(unit or '?')}"
        if title:
            label += f" — {escape(title)}"
    return f"{label} <code>{group['group_id']}</code>"


def _report(day: date, sent: int, misses: list[tuple[dict, str]]) -> str:
    lines = [f"📣 <b>Morning PTI call — {day:%a %d %b}</b>",
             f"Posted to {sent} group(s)."]
    if misses:
        lines += ["", f"⚠️ <b>{len(misses)} group(s) did not get it:</b>"]
        for group, reason in misses[:REPORT_LIMIT]:
            lines.append(f"• {_group_label(group)}\n  <i>{escape(reason)}</i>")
        if len(misses) > REPORT_LIMIT:
            lines.append(f"…and {len(misses) - REPORT_LIMIT} more.")
    return "\n".join(lines)


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
    sent = 0
    misses: list[tuple[dict, str]] = []
    migrated = False
    for group in await get_broadcast_targets():
        group_id = group["group_id"]
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
            reason = str(e) or type(e).__name__
            misses.append((group, reason))
            await note_send_failure(group_id, e)
            logging.warning("PTI broadcast to %s failed: %s: %s",
                            group_id, type(e).__name__, reason)
        await asyncio.sleep(PAUSE_SECONDS)

    # A group that moved to a new id still has to be reached, so a run that
    # followed one is not finished: the next tick claims and posts the new id.
    if not migrated:
        _done = day
    if sent or misses:
        logging.info("PTI broadcast for %s: posted to %s group(s), %s failed",
                     day, sent, len(misses))
        await notify_admins_named(PTI_BROADCAST_REPORT_ADMIN,
                                  _report(day, sent, misses))
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
