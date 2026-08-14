"""One reminder per unit per 24 hours, whichever sender is asking.

Drives ``reminders._process_group`` with the DB and Telegram calls faked out.
The pass runs hourly, so everything here is about what a group hears in a day.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from utils import reminders

# A Monday at 15:00 UTC — a twice-weekly reminder day, past the 14:00 cutoff.
MON_15 = datetime(2026, 6, 22, 15, 0)


def _env(monkeypatch, sent, drivers=None):
    async def fake_send(group_id, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(reminders.bot, "send_message", fake_send)
    monkeypatch.setattr(reminders, "get_drivers",
                        AsyncMock(return_value=drivers or [{"user_id": 1, "name": "Bob"}]))
    monkeypatch.setattr(reminders, "get_last_pti_for_group", AsyncMock(return_value=None))
    for name in ("mark_overdue_reminded", "mark_escalation_reminded",
                 "mark_weekly_reminder", "mark_reminder_sent", "reset_group_reminders",
                 "clear_unreachable", "mark_unreachable"):
        monkeypatch.setattr(reminders, name, AsyncMock())
    monkeypatch.setattr(reminders, "send_overdue_alert", AsyncMock())


def test_an_overdue_unit_gets_one_message_on_a_weekly_day(monkeypatch):
    # Both #8 and #9 come due in the same pass. The overdue notice is the one
    # worth the day's slot -- "send one twice a week" tells an overdue driver
    # nothing they don't already know.
    sent = []
    _env(monkeypatch, sent)
    group = {"group_id": -100, "created_at": MON_15 - timedelta(days=5),
             "overdue_reminded_at": None, "last_escalation_at": None,
             "last_weekly_reminder_on": None, "last_reminder_at": None}

    asyncio.run(reminders._process_group(group, MON_15))

    assert len(sent) == 1
    assert "No PTI in" in sent[0]


def test_nothing_goes_out_within_the_day(monkeypatch):
    sent = []
    _env(monkeypatch, sent)
    group = {"group_id": -100, "created_at": MON_15 - timedelta(days=9),
             "overdue_reminded_at": MON_15 - timedelta(days=4),
             "last_escalation_at": MON_15 - timedelta(days=2),
             "last_weekly_reminder_on": None,
             "last_reminder_at": MON_15 - timedelta(hours=3)}

    asyncio.run(reminders._process_group(group, MON_15))

    assert sent == []


def test_the_escalation_returns_the_next_day(monkeypatch):
    sent = []
    _env(monkeypatch, sent)
    group = {"group_id": -100, "created_at": MON_15 - timedelta(days=9),
             "overdue_reminded_at": MON_15 - timedelta(days=4),
             "last_escalation_at": MON_15 - timedelta(hours=25),
             "last_weekly_reminder_on": MON_15.date(),
             "last_reminder_at": MON_15 - timedelta(hours=25)}

    asyncio.run(reminders._process_group(group, MON_15))

    assert len(sent) == 1
    assert "still overdue" in sent[0]


def test_a_compliant_unit_still_gets_its_weekly_nudge(monkeypatch):
    # The daily cap is a ceiling, not a mute: a group that isn't overdue and
    # wasn't messaged yesterday still hears the twice-weekly reminder.
    sent = []
    _env(monkeypatch, sent)
    group = {"group_id": -100, "created_at": MON_15 - timedelta(days=1),
             "overdue_reminded_at": None, "last_escalation_at": None,
             "last_weekly_reminder_on": None,
             "last_reminder_at": MON_15 - timedelta(hours=30)}

    asyncio.run(reminders._process_group(group, MON_15))

    assert len(sent) == 1
    assert "PTI reminder" in sent[0]


def test_a_fresh_pti_still_clears_the_overdue_state_inside_the_day(monkeypatch):
    # The reset is bookkeeping, not a message, so the spent daily slot must not
    # leave the group stuck in the escalation phase.
    sent = []
    _env(monkeypatch, sent)
    group = {"group_id": -100, "created_at": MON_15 - timedelta(days=9),
             "overdue_reminded_at": MON_15 - timedelta(days=4),
             "last_escalation_at": MON_15 - timedelta(days=1),
             "last_weekly_reminder_on": MON_15.date(),
             "last_reminder_at": MON_15 - timedelta(hours=2)}
    monkeypatch.setattr(reminders, "get_last_pti_for_group",
                        AsyncMock(return_value={"submitted_at": MON_15 - timedelta(hours=1)}))

    asyncio.run(reminders._process_group(group, MON_15))

    assert sent == []
    reminders.reset_group_reminders.assert_awaited_once()
