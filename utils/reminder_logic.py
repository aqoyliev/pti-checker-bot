"""Pure decision logic for the PTI reminder engine (#8 / #9).

No aiogram / DB imports on purpose, so it can be unit-tested in isolation.
``utils/reminders.py`` wraps these with the Telegram + DB side effects.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

# #8 — twice a week, on fixed days. Python weekday(): Mon=0 … Sun=6.
WEEKLY_REMINDER_DAYS = {0, 3}              # Monday & Thursday
WEEKLY_REMINDER_HOUR_UTC = 14             # don't fire before 14:00 UTC on those days

# #9 — overdue escalation.
OVERDUE_DAYS = 3                          # quiet for this long → first reminder
ESCALATION_GRACE = timedelta(days=1)      # one day after the first reminder before escalating
ESCALATION_INTERVAL = timedelta(hours=24)  # then nag this often until a PTI lands

# One reminder per unit per 24 hours, whichever kind it is. The escalation
# interval above sets the pace of the overdue nag; this is the floor underneath
# *every* reminder, so a unit can't collect a twice-weekly nudge and an overdue
# notice in the same day, and two senders can't each think they're within their
# own budget. A driver who has been told once today has been told.
MIN_REMINDER_GAP = timedelta(hours=24)


def may_remind(
    now: datetime,
    last_reminder_at: datetime | None,
    gap: timedelta = MIN_REMINDER_GAP,
) -> bool:
    """True if this group's 24-hour reminder slot is free.

    ``last_reminder_at`` is the last reminder of *any* kind posted to the group.
    None means it has never been reminded, which is always allowed.
    """
    if last_reminder_at is None:
        return True
    return now - last_reminder_at >= gap


def decide_weekly(
    now: datetime,
    last_weekly_on: date | None,
    days: set[int] = WEEKLY_REMINDER_DAYS,
    hour_utc: int = WEEKLY_REMINDER_HOUR_UTC,
) -> bool:
    """True if the twice-weekly nudge is due now and hasn't been sent today."""
    if now.weekday() not in days:
        return False
    if now.hour < hour_utc:
        return False
    return last_weekly_on != now.date()


def decide_overdue_action(
    now: datetime,
    reference: datetime,
    overdue_reminded_at: datetime | None,
    last_escalation_at: datetime | None,
    overdue_days: int = OVERDUE_DAYS,
    grace: timedelta = ESCALATION_GRACE,
    interval: timedelta = ESCALATION_INTERVAL,
) -> str:
    """Decide the overdue action for one group.

    ``reference`` is the time of the last PTI (or the group's creation time if it
    never sent one). Returns one of:
      "none"          — nothing to do
      "reset"         — a PTI is recent again; clear any overdue/escalation state
      "overdue_first" — just crossed `overdue_days`; send the single first notice
      "escalation"    — in the every-`interval` phase; send a nag now
    """
    if now - reference < timedelta(days=overdue_days):
        return "reset" if (overdue_reminded_at or last_escalation_at) else "none"

    # Overdue.
    if overdue_reminded_at is None:
        return "overdue_first"
    if now - overdue_reminded_at < grace:
        return "none"  # the one-day grace after the first notice
    if last_escalation_at is None or now - last_escalation_at >= interval:
        return "escalation"
    return "none"
