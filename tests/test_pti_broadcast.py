"""The Monday / Wednesday / Friday 07:00 Eastern PTI call.

Pins when it fires (local time, through daylight saving), that every group is
claimed before it is posted to so a restart can never post twice, that the
picture is uploaded once rather than once per group, and that a failed post
neither stops the run nor touches the reminder engine's bookkeeping.
"""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.utils.exceptions import BadRequest, MigrateToChat

from utils import pti_broadcast as pb

ET = ZoneInfo("America/New_York")


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ---------- when ----------

@pytest.mark.parametrize("now, expected", [
    # Monday 12 Oct 2026 is daylight time: 07:00 ET is 11:00 UTC.
    (_utc(2026, 10, 12, 10, 59), None),
    (_utc(2026, 10, 12, 11, 0), date(2026, 10, 12)),
    (_utc(2026, 10, 12, 12, 59), date(2026, 10, 12)),
    (_utc(2026, 10, 12, 13, 0), None),  # 09:00 ET: too late to be a morning call
    # Wednesday and Friday of the same week.
    (_utc(2026, 10, 14, 11, 30), date(2026, 10, 14)),
    (_utc(2026, 10, 16, 11, 30), date(2026, 10, 16)),
    # Tuesday, Thursday, Saturday, Sunday: never.
    (_utc(2026, 10, 13, 11, 30), None),
    (_utc(2026, 10, 15, 11, 30), None),
    (_utc(2026, 10, 17, 11, 30), None),
    (_utc(2026, 10, 18, 11, 30), None),
    # Monday 11 Jan 2027 is standard time: 07:00 ET is 12:00 UTC.
    (_utc(2027, 1, 11, 11, 30), None),
    (_utc(2027, 1, 11, 12, 0), date(2027, 1, 11)),
])
def test_due_at_seven_eastern_on_mon_wed_fri(now, expected):
    assert pb.due_day(now, ET) == expected


def test_the_day_is_the_local_one():
    """Friday 07:30 ET in winter is 12:30 UTC -- but a UTC clock past midnight
    must never be read as the next local day."""
    late_thursday_et = _utc(2027, 1, 15, 3, 0)  # Thu 22:00 ET
    assert pb.due_day(late_thursday_et, ET) is None


# ---------- the message ----------

def test_the_caption_is_the_safety_teams_text_escaped():
    assert pb.CAPTION.startswith("Good morning.")
    assert "Safety team" in pb.CAPTION
    # Every message is HTML; a bare ampersand would be refused.
    assert "safe &amp; DOT" in pb.CAPTION
    assert len(pb.CAPTION) <= 1024  # Telegram's caption limit


def test_the_picture_ships_with_the_code():
    assert pb.IMAGE.is_file()


# ---------- the run ----------

MON = _utc(2026, 10, 12, 11, 5)


def _wire(monkeypatch, *, targets=(-1, -2, -3), taken=()):
    pb._done = None
    pb._photo_id = None
    claims = []

    async def claim(kind, day, gid):
        claims.append((kind, day, gid))
        return gid not in taken

    monkeypatch.setattr(pb, "get_broadcast_targets",
                        AsyncMock(return_value=list(targets)))
    monkeypatch.setattr(pb, "claim_broadcast", claim)
    monkeypatch.setattr(pb, "prune_broadcast_sent", AsyncMock())
    monkeypatch.setattr(pb, "migrate_group_id", AsyncMock(return_value=True))
    monkeypatch.setattr(pb, "note_send_ok", AsyncMock())
    failures = AsyncMock()
    monkeypatch.setattr(pb, "note_send_failure", failures)
    monkeypatch.setattr(pb, "PAUSE_SECONDS", 0)
    sent = AsyncMock(return_value=SimpleNamespace(
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="BIG")]))
    monkeypatch.setattr(pb.bot, "send_photo", sent)
    return sent, claims, failures


def test_every_target_gets_the_picture_and_caption(monkeypatch):
    sent, claims, _ = _wire(monkeypatch)

    assert asyncio.run(pb.run_broadcast_if_due(MON)) == 3

    assert [c.args[0] for c in sent.await_args_list] == [-1, -2, -3]
    assert all(c.kwargs["caption"] == pb.CAPTION for c in sent.await_args_list)
    assert [c[2] for c in claims] == [-1, -2, -3]
    assert {c[1] for c in claims} == {date(2026, 10, 12)}


def test_the_picture_is_uploaded_once(monkeypatch):
    """After the first post the photo goes by file_id, not as a fresh upload
    to each of 150 groups."""
    sent, _, _ = _wire(monkeypatch)

    asyncio.run(pb.run_broadcast_if_due(MON))

    first, *rest = [c.args[1] for c in sent.await_args_list]
    assert not isinstance(first, str)
    assert rest == ["BIG", "BIG"]


def test_a_group_already_claimed_today_is_not_posted_to_again(monkeypatch):
    """What a restart in the middle of a run finds."""
    sent, _, _ = _wire(monkeypatch, taken={-1, -2})

    assert asyncio.run(pb.run_broadcast_if_due(MON)) == 1
    assert [c.args[0] for c in sent.await_args_list] == [-3]


def test_outside_the_window_nothing_is_read_or_sent(monkeypatch):
    sent, claims, _ = _wire(monkeypatch)

    assert asyncio.run(pb.run_broadcast_if_due(_utc(2026, 10, 13, 11, 5))) == 0
    pb.get_broadcast_targets.assert_not_awaited()
    sent.assert_not_awaited()


def test_a_finished_run_is_not_repeated_within_the_window(monkeypatch):
    sent, _, _ = _wire(monkeypatch)

    asyncio.run(pb.run_broadcast_if_due(MON))
    asyncio.run(pb.run_broadcast_if_due(_utc(2026, 10, 12, 11, 6)))

    assert sent.await_count == 3
    assert pb.get_broadcast_targets.await_count == 1


def test_a_failed_post_is_recorded_and_the_run_goes_on(monkeypatch):
    sent, _, failures = _wire(monkeypatch)
    ok = sent.return_value
    sent.side_effect = [ok, BadRequest("have no rights to send a message"), ok]

    assert asyncio.run(pb.run_broadcast_if_due(MON)) == 2
    assert failures.await_args.args[0] == -2


def test_a_migrated_group_is_followed_and_reached_next_tick(monkeypatch):
    sent, _, _ = _wire(monkeypatch, targets=(-1,))
    sent.side_effect = MigrateToChat(-1009)

    asyncio.run(pb.run_broadcast_if_due(MON))

    pb.migrate_group_id.assert_awaited_once_with(-1, -1009)
    # The run is left open so the new id is claimed and posted on the next tick.
    assert pb._done is None


def test_it_never_touches_the_reminder_cap():
    """Stamping last_reminder_at three mornings a week would hold the overdue
    escalation off for most of the week."""
    source = pb.Path(pb.__file__).read_text(encoding="utf-8")
    code = source.split('"""', 2)[2]  # past the module docstring
    assert "last_reminder_at" not in code
    assert "mark_unreachable" not in code
    assert "mark_group_inactive" not in code
