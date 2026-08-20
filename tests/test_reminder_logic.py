"""Pure tests for the reminder decision logic (utils/reminder_logic.py).

No DB / network / aiogram — just the state machine for #9 (3-day overdue →
next-day, then once a day), plus the one-reminder-per-unit-per-24-hours floor
underneath it.

The twice-weekly nudge (#8, decide_weekly) that used to live here was removed
2026-08-20 -- it fired on a fixed Mon/Thu schedule with no regard for whether a
PTI had already come in that day. Don't reintroduce it.
"""
from datetime import datetime, timedelta

from utils.reminder_logic import (
    ESCALATION_INTERVAL,
    MIN_REMINDER_GAP,
    decide_overdue_action,
    may_remind,
)

MON_15 = datetime(2026, 6, 22, 15, 0)


# ---------- decide_overdue_action (#9) ----------

def test_recent_pti_no_state_is_none():
    now = datetime(2026, 6, 24, 12, 0)
    recent = now - timedelta(days=1)
    assert decide_overdue_action(now, recent, None, None) == "none"


def test_recent_pti_with_stale_state_resets():
    now = datetime(2026, 6, 24, 12, 0)
    recent = now - timedelta(days=1)
    assert decide_overdue_action(now, recent, now - timedelta(days=5), None) == "reset"


def test_just_crossed_three_days_sends_first_notice():
    now = datetime(2026, 6, 24, 12, 0)
    reference = now - timedelta(days=3, hours=1)
    assert decide_overdue_action(now, reference, None, None) == "overdue_first"


def test_within_one_day_grace_after_first_notice_is_quiet():
    now = datetime(2026, 6, 24, 12, 0)
    reference = now - timedelta(days=4)
    reminded = now - timedelta(hours=5)  # < 1 day ago
    assert decide_overdue_action(now, reference, reminded, None) == "none"


def test_next_day_starts_escalation():
    now = datetime(2026, 6, 24, 12, 0)
    reference = now - timedelta(days=5)
    reminded = now - timedelta(days=1, hours=1)  # > 1 day ago, no escalation yet
    assert decide_overdue_action(now, reference, reminded, None) == "escalation"


def test_escalation_is_once_a_day():
    now = datetime(2026, 6, 24, 12, 0)
    reference = now - timedelta(days=5)
    reminded = now - timedelta(days=2)
    recent_esc = now - timedelta(hours=23)      # < 24h → quiet
    assert decide_overdue_action(now, reference, reminded, recent_esc) == "none"
    old_esc = now - timedelta(hours=24, minutes=1)  # >= 24h → fire
    assert decide_overdue_action(now, reference, reminded, old_esc) == "escalation"


# ---------- may_remind: one reminder per unit per 24 hours ----------

def test_a_unit_never_reminded_may_be_reminded():
    assert may_remind(MON_15, None) is True


def test_a_unit_reminded_hours_ago_is_left_alone():
    # The pass runs hourly; without this the same unit is told 24 times a day.
    assert may_remind(MON_15, MON_15 - timedelta(hours=5)) is False


def test_the_slot_reopens_after_a_full_day():
    assert may_remind(MON_15, MON_15 - timedelta(hours=24)) is True
    assert may_remind(MON_15, MON_15 - timedelta(hours=23, minutes=59)) is False


def test_the_gap_is_the_escalation_interval():
    # Both are 24h on purpose: the escalation sets the pace, the gap is the
    # floor under every kind of reminder. A shorter gap would let the weekly
    # nudge slip in beside an overdue nag.
    assert MIN_REMINDER_GAP == ESCALATION_INTERVAL
