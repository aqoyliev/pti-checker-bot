"""Can the bot still post in a driver's group? Tell the admins when it can't.

A group admin who removes the bot's "Send Messages" permission breaks the bot
**silently**. The bot keeps receiving every message, so nothing looks wrong from
the inside: `/check` still runs the inspection, the result just never lands, and
the overdue reminder fails the same way. The drivers see a bot that stopped
answering, the fleet sees a unit that stopped inspecting, and the deployment
sees a log line nobody reads. So this reports it out loud, to the people who
can ask for the permission back.

**Being kicked is not this.** It is a different situation with a different
answer -- the bot must be re-added, and there is already machinery for it
(``mark_unreachable``: three consecutive failures retire the group, deliberately
slow because the local Bot API server forgets chats when it restarts). Alerting
on it too would mean an alert for every group anyone ever tidied up.

Two detectors, because they fail differently:

* **Reactive** — a send that comes back "no rights". Free, immediate, and it is
  the exact event the admin needs to know about. But it only fires when there
  was something to send, and a group with a compliant driver gets nothing sent
  to it for days.
* **Proactive** — one ``getChatMember`` per active group, once a day, alongside
  the title sweep. ~150 calls a day catches a restriction the day it happens
  rather than whenever a reminder next comes due.

Both funnel through ``record_post_access``, which writes ``groups.post_blocked``
and only messages the admins when that value *changes*. An alert repeated every
hour is an alert nobody reads.
"""
from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram.utils.exceptions import TelegramAPIError, Unauthorized

from utils.admins import notify_super_admins
from utils.db import get_all_groups, get_group, set_post_blocked

# One call per group, spaced like the title sweep's: a burst of 150 getChatMember
# calls is exactly the shape of request Telegram rate-limits.
_CHECK_DELAY = 0.3

# What Telegram says when the bot is in the chat but muted. There is no aiogram
# exception class for it (it arrives as a bare BadRequest), so the text is the
# only signal, and it is matched loosely because the Bot API has worded it more
# than one way over the years.
_NO_RIGHTS = (
    "have no rights to send a message",
    "not enough rights to send text messages",
    "not enough rights to send messages",
    "chat_write_forbidden",
    "chat_send_plain_forbidden",
)


def is_post_denied(exc: BaseException) -> bool:
    """True when a failed send means "the bot is muted here", not "it is gone".

    Pure, so the distinction that the whole module rests on can be tested
    without Telegram. ``Unauthorized`` (which covers BotKicked and BotBlocked)
    is explicitly *not* this: a bot that was removed cannot be un-muted, and
    saying so would send an admin to look for a permission that isn't there.
    """
    if isinstance(exc, Unauthorized):
        return False
    if not isinstance(exc, TelegramAPIError):
        return False
    text = str(exc).lower()
    return any(fragment in text for fragment in _NO_RIGHTS)


def group_label(group: dict | None, group_id: int) -> str:
    unit = ((group or {}).get("unit_number") or "").strip()
    title = ((group or {}).get("title") or "").strip() or str(group_id)
    return f"{unit} — {title}" if unit else title


async def record_post_access(group_id: int, blocked: bool,
                             group: dict | None = None) -> None:
    """Store whether the bot can post here, and alert on a change of answer.

    ``group`` is only for the alert's wording; the sweep already holds the row,
    and the reactive path reads it here rather than threading a dict through
    every sender for the sake of a label that is needed twice a year.
    """
    if not await set_post_blocked(group_id, blocked):
        return   # already knew; say nothing

    if group is None:
        try:
            group = await get_group(group_id)
        except Exception:
            group = None
    label = escape(group_label(group, group_id))
    if blocked:
        logging.warning("group %s: the bot is no longer allowed to post", group_id)
        await notify_super_admins(
            "🔇 <b>The bot can't post in this group</b>\n"
            f"{label}\n\n"
            "It is still a member, so its <b>Send Messages</b> permission was "
            "taken away. Inspection results and reminders will not reach the "
            "drivers until it is given back.\n"
            "<i>Fix: group settings → Administrators / Permissions → allow the "
            "bot to send messages.</i>"
        )
    else:
        logging.info("group %s: posting works again", group_id)
        await notify_super_admins(f"🔊 <b>Posting works again</b>\n{label}")


async def note_send_failure(group_id: int, exc: BaseException) -> None:
    """Reactive detector: call this from a sender that just failed."""
    if is_post_denied(exc):
        await record_post_access(group_id, True)


async def note_send_ok(group_id: int) -> None:
    """A message got through, so whatever was in the way is gone."""
    await record_post_access(group_id, False)


async def run_post_access_sweep() -> int:
    """Proactive detector: ask Telegram once a day. Returns the blocked count.

    Reads the bot's own membership rather than trying a message, because the
    check must not put anything in a driver's group. Three answers matter:

    * **left / kicked** — not this module's business (above), and deliberately
      not recorded as blocked either: the group is either coming back or the
      unreachable strikes will retire it.
    * **restricted** — Telegram states ``can_send_messages`` outright.
    * **member** — the bot holds whatever the group's *default* permissions
      are, so those are what decide. A group that mutes everyone by default
      mutes the bot too; only an admin is exempt.

    A group that can't be read at all is skipped, on the same reasoning as the
    title sweep: "couldn't ask" must never be filed as an answer.
    """
    from loader import bot, bot_id

    me = await bot_id()
    blocked_now = 0
    for group in [g for g in await get_all_groups() if g.get("is_active", True)]:
        gid = group["group_id"]
        try:
            member = await bot.get_chat_member(gid, me)
            status = getattr(member, "status", "")
            if status in ("left", "kicked"):
                continue
            if status == "restricted":
                allowed = bool(getattr(member, "can_send_messages", False))
            elif status in ("administrator", "creator"):
                allowed = True
            else:
                chat = await bot.get_chat(gid)
                default = getattr(getattr(chat, "permissions", None),
                                  "can_send_messages", None)
                # No permissions object at all (a basic group Telegram reports
                # nothing for) means nothing is restricted.
                allowed = True if default is None else bool(default)
        except Exception as exc:
            logging.info("post-access sweep skipped %s: %s", gid, type(exc).__name__)
            continue

        await record_post_access(gid, not allowed, group)
        blocked_now += 0 if allowed else 1
        await asyncio.sleep(_CHECK_DELAY)

    logging.info("post-access sweep finished: %s group(s) cannot be posted in",
                 blocked_now)
    return blocked_now
