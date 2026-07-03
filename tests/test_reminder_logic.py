"""Pure tests for the reminder decision logic (utils/reminder_logic.py).

No DB / network / aiogram — just the state machine for #8 (twice-weekly) and
#9 (3-day overdue → next-day every-12-hours escalation).
"""
from datetime import datetime, timedelta

from utils.reminder_logic import decide_overdue_action, decide_weekly

# A Monday at 15:00 UTC (Mon=0 is a reminder day, 15 >= the 14:00 cutoff).
MON_15 = datetime(2026, 6, 22, 15, 0)


# ---------- decide_weekly (#8) ----------

def test_weekly_fires_on_reminder_day_after_hour_when_not_sent_today():
    assert decide_weekly(MON_15, last_weekly_on=None) is True


def test_weekly_silent_before_cutoff_hour():
    assert decide_weekly(MON_15.replace(hour=9), last_weekly_on=None) is False


def test_weekly_silent_on_non_reminder_day():
    tuesday = MON_15 + timedelta(days=1)  # Tue, not in {Mon, Thu}
    assert decide_weekly(tuesday, last_weekly_on=None) is False


def test_weekly_not_resent_same_day():
    assert decide_weekly(MON_15, last_weekly_on=MON_15.date()) is False


def test_weekly_fires_on_thursday():
    thursday = MON_15 + timedelta(days=3)
    assert decide_weekly(thursday, last_weekly_on=None) is True


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


def test_escalation_respects_twelve_hour_interval():
    now = datetime(2026, 6, 24, 12, 0)
    reference = now - timedelta(days=5)
    reminded = now - timedelta(days=2)
    recent_esc = now - timedelta(hours=11)      # < 12h → quiet
    assert decide_overdue_action(now, reference, reminded, recent_esc) == "none"
    old_esc = now - timedelta(hours=12, minutes=1)  # >= 12h → fire
    assert decide_overdue_action(now, reference, reminded, old_esc) == "escalation"
