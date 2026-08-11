"""Is a group still alive? — derived from real human traffic, not a flag.

``groups.is_active`` is a *manual/administrative* flag and a poor proxy for
"this truck's chat is in use": it was set FALSE wholesale by a send failure once
(19 of 30 deactivated groups still had the bot in them), and an admin who never
revisits the panel leaves it TRUE forever.

So activity is measured the way ``scripts/tg_scan.py`` measures it offline: the
timestamp of the last message from a *human*. Bot chatter — PTI results,
reminders — doesn't count, or every group the bot nags would look alive; nor do
join/leave/pin service messages. ``middlewares/group_activity.py`` stamps
``groups.last_human_message_at`` from live traffic; this module is the pure
"how old is too old" half so it can be unit-tested without a DB.

Deliberately *derived* and read-only:

- it never writes ``is_active`` (that stays the admin's switch), and
- it never suppresses a reminder. A group with no human message for three days
  is precisely the group the overdue reminder exists to poke; gating reminders
  on this would neuter the feature for the drivers who most need it.

It is a reporting status: the admin panel and stats say "dormant" so a fleet
manager can tell a quiet truck from a busy one at a glance.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from data.config import GROUP_INACTIVE_DAYS


def _naive_utc(dt: datetime | None) -> datetime | None:
    """DB timestamps are naive UTC; tolerate tz-aware ones from other callers."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def last_human_at(group: dict) -> datetime | None:
    """When a human last posted, or None if the bot has never seen one."""
    return _naive_utc(group.get("last_human_message_at"))


def quiet_for(group: dict, now: datetime | None = None) -> timedelta | None:
    """How long the group has been silent, or None when that is unknown.

    Unknown means no human message has been recorded *and* the group has no
    creation time to fall back on — not "zero".
    """
    now = now or datetime.utcnow()
    reference = last_human_at(group) or _naive_utc(group.get("created_at"))
    if reference is None:
        return None
    return max(now - reference, timedelta(0))


def quiet_days(group: dict, now: datetime | None = None) -> int | None:
    q = quiet_for(group, now)
    return None if q is None else q.days


def is_dormant(group: dict, now: datetime | None = None,
               days: int = GROUP_INACTIVE_DAYS) -> bool:
    """True when no human has posted in the group for ``days`` days.

    A group that has never spoken falls back to its creation time, so a group
    added this morning reads as alive (too young to judge) while one that has
    been silent since it was added reads as dormant. With neither timestamp
    there is no evidence either way, and the group is left alone (False) —
    unknown is not the same as dead, and this status is shown to admins.
    """
    q = quiet_for(group, now)
    return q is not None and q >= timedelta(days=days)


def describe_quiet(group: dict, now: datetime | None = None) -> str:
    """Short human-readable idle time for the admin views ('2h', '6d', '—')."""
    q = quiet_for(group, now)
    if q is None:
        return "—"
    if q < timedelta(hours=1):
        return f"{int(q.total_seconds() // 60)}m"
    if q < timedelta(days=1):
        return f"{int(q.total_seconds() // 3600)}h"
    return f"{q.days}d"
