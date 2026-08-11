"""Which truck chats have gone quiet — derived from traffic, not from a flag.

``groups.is_active`` is an administrative switch: the weekly ``/units`` sweep and
the admin panel set it. It answers "should this group still be running?", not
"is anyone actually using it?" — a group stays TRUE until someone acts.

Quiet is measured instead from real human traffic:
``middlewares/group_activity.py`` counts one per human message into a daily
bucket, and a group is quiet when at most ``GROUP_QUIET_MAX_MESSAGES`` of them
landed over the last ``GROUP_QUIET_DAYS`` days. The threshold is a count rather
than zero on purpose — a stray "ok", a sticker or a thumbs-up is not evidence a
truck is in service, so a couple of messages still reads as quiet.

This module is the pure half, so the rule can be unit-tested without a DB.

Deliberately read-only:

- it never writes ``is_active``. Deactivation belongs to the weekly units sweep,
  where a human confirms it; a chat being quiet is evidence, not a decision.
- it never suppresses a reminder. A group nobody has posted in for three days is
  precisely the group the overdue reminder exists to poke, and a silent truck is
  a missing inspection — so quiet groups stay in the compliance denominator too.
"""
from __future__ import annotations

from data.config import GROUP_QUIET_DAYS, GROUP_QUIET_MAX_MESSAGES

__all__ = ["GROUP_QUIET_DAYS", "is_quiet", "quiet_groups"]


def is_quiet(count: int | None, max_messages: int = GROUP_QUIET_MAX_MESSAGES) -> bool:
    """True when this many human messages counts as "nobody is using the chat".

    ``None`` means no bucket exists at all — a group that has never spoken — and
    is quiet, not unknown.
    """
    return (count or 0) <= max_messages


def quiet_groups(
    groups: list[dict],
    counts: dict[int, int],
    max_messages: int = GROUP_QUIET_MAX_MESSAGES,
) -> list[dict]:
    """The active groups that have gone quiet, noisiest last.

    Deactivated groups are left out: they are already known-retired, so listing
    them would bury the live trucks that just went silent — which are the only
    ones worth looking at.
    """
    out = [
        g for g in groups
        if g.get("is_active", True) and is_quiet(counts.get(g["group_id"]), max_messages)
    ]
    out.sort(key=lambda g: (counts.get(g["group_id"], 0), str(g.get("unit_number") or "")))
    return out
