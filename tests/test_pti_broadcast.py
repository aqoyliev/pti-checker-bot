"""The Monday / Wednesday / Friday 07:00 Eastern PTI call.

Pins when it fires (local time, through daylight saving), that every group is
claimed before it is posted to so a restart can never post twice, that the
picture always goes by file_id -- uploaded once, ever, per picture -- and that
a group it did not reach is named to Safety Monitoring with Telegram's reason,
while the run goes on and the reminder engine's bookkeeping is left alone.
"""
import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.utils.exceptions import BadRequest, MigrateToChat

from utils import admins
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
    """A UTC clock past midnight must never be read as the next local day."""
    late_thursday_et = _utc(2027, 1, 15, 3, 0)  # Thu 22:00 ET
    assert pb.due_day(late_thursday_et, ET) is None


# ---------- the message ----------

def test_the_caption_is_the_safety_teams_text_escaped():
    assert pb.CAPTION.startswith("Good morning.")
    assert "Safety team" in pb.CAPTION
    # Every message is HTML; a bare ampersand would be refused.
    assert "safe &amp; DOT" in pb.CAPTION
    assert len(pb.CAPTION) <= 1024  # Telegram's caption limit


def test_the_caption_has_no_stray_space():
    assert "hoses)" in pb.CAPTION and "hoses )" not in pb.CAPTION


def test_the_picture_ships_with_the_code():
    assert pb.IMAGE.is_file()


def test_the_picture_reaches_the_runtime_image():
    """The first deploy kept it under assets/, which .dockerignore excludes,
    so the container had no picture and the broadcast switched itself off."""
    root = pb.IMAGE.parents[1]
    rel = pb.IMAGE.relative_to(root).as_posix()
    ignored = [line.strip().rstrip("/") for line in
               (root / ".dockerignore").read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("#")]
    parts = rel.split("/")
    prefixes = {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}
    assert not prefixes & set(ignored), rel


# ---------- the run ----------

MON = _utc(2026, 10, 12, 11, 5)


def _group(gid, unit=None, title=""):
    return {"group_id": gid, "unit_number": unit or str(-gid), "title": title}


def _wire(monkeypatch, *, targets=None, taken=(), stored=None):
    """Stub the database, the bot and the report. `stored` is what
    app_settings holds for the file_id, already in its digest-prefixed form."""
    pb._done = None
    pb._photo_id = None
    pb._photo_loaded = False
    claims = []
    targets = [_group(-1), _group(-2), _group(-3)] if targets is None else targets

    async def claim(kind, day, gid):
        claims.append((kind, day, gid))
        return gid not in taken

    settings = {pb.FILE_ID_SETTING: stored} if stored else {}

    async def get_setting(key):
        return settings.get(key)

    async def set_setting(key, value):
        settings[key] = value

    monkeypatch.setattr(pb, "get_broadcast_targets", AsyncMock(return_value=targets))
    monkeypatch.setattr(pb, "claim_broadcast", claim)
    monkeypatch.setattr(pb, "prune_broadcast_sent", AsyncMock())
    monkeypatch.setattr(pb, "migrate_group_id", AsyncMock(return_value=True))
    monkeypatch.setattr(pb, "get_setting", get_setting)
    monkeypatch.setattr(pb, "set_setting", set_setting)
    monkeypatch.setattr(pb, "note_send_ok", AsyncMock())
    failures = AsyncMock()
    monkeypatch.setattr(pb, "note_send_failure", failures)
    monkeypatch.setattr(pb, "PAUSE_SECONDS", 0)
    monkeypatch.setattr(pb, "notify_admins_named", AsyncMock(return_value=1))
    photo = AsyncMock(return_value=SimpleNamespace(
        message_id=77,
        photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="BIG")]))
    monkeypatch.setattr(pb.bot, "send_photo", photo)
    copied = AsyncMock()
    monkeypatch.setattr(pb.bot, "copy_message", copied, raising=False)
    return SimpleNamespace(photo=photo, copy=copied, claims=claims,
                           failures=failures, settings=settings)


def _sent_to(w):
    return [c.args[0] for c in w.photo.await_args_list]


def _report(w) -> str:
    return pb.notify_admins_named.await_args.args[1]


def test_every_target_gets_the_picture_and_caption(monkeypatch):
    w = _wire(monkeypatch)

    assert asyncio.run(pb.run_broadcast_if_due(MON)) == 3

    assert _sent_to(w) == [-1, -2, -3]
    assert all(c.kwargs["caption"] == pb.CAPTION for c in w.photo.await_args_list)
    assert [c[2] for c in w.claims] == [-1, -2, -3]
    assert {c[1] for c in w.claims} == {date(2026, 10, 12)}


def test_the_picture_is_uploaded_once_then_always_sent_by_file_id(monkeypatch):
    w = _wire(monkeypatch)

    asyncio.run(pb.run_broadcast_if_due(MON))

    first, *rest = [c.args[1] for c in w.photo.await_args_list]
    assert not isinstance(first, str)  # the one upload
    assert rest == ["BIG", "BIG"]
    w.copy.assert_not_awaited()  # never by message_id


def test_the_file_id_outlives_a_restart(monkeypatch):
    """Stored with the picture's digest, so the next process -- or the next
    Monday -- uploads nothing at all."""
    stored = f"{pb._image_digest()} SAVED"
    w = _wire(monkeypatch, stored=stored)

    asyncio.run(pb.run_broadcast_if_due(MON))

    assert [c.args[1] for c in w.photo.await_args_list] == ["SAVED"] * 3


def test_a_first_upload_is_saved_for_next_time(monkeypatch):
    w = _wire(monkeypatch)

    asyncio.run(pb.run_broadcast_if_due(MON))

    assert w.settings[pb.FILE_ID_SETTING] == f"{pb._image_digest()} BIG"


def test_a_new_picture_is_uploaded_rather_than_answered_with_the_old_one(
        monkeypatch):
    w = _wire(monkeypatch, stored="0000000000000000 OLD")

    asyncio.run(pb.run_broadcast_if_due(MON))

    first = w.photo.await_args_list[0].args[1]
    assert first != "OLD" and not isinstance(first, str)


def test_a_refused_file_id_costs_one_upload(monkeypatch):
    w = _wire(monkeypatch, stored=f"{pb._image_digest()} STALE",
              targets=[_group(-1), _group(-2)])
    ok = w.photo.return_value
    w.photo.side_effect = [BadRequest("Wrong file identifier/http url specified"),
                           ok, ok]

    assert asyncio.run(pb.run_broadcast_if_due(MON)) == 2

    args = [c.args[1] for c in w.photo.await_args_list]
    assert args[0] == "STALE" and not isinstance(args[1], str) and args[2] == "BIG"
    w.failures.assert_not_awaited()


def test_a_group_already_claimed_today_is_not_posted_to_again(monkeypatch):
    """What a restart in the middle of a run finds."""
    w = _wire(monkeypatch, taken={-1, -2})

    assert asyncio.run(pb.run_broadcast_if_due(MON)) == 1
    assert _sent_to(w) == [-3]


def test_outside_the_window_nothing_is_read_or_sent(monkeypatch):
    w = _wire(monkeypatch)

    assert asyncio.run(pb.run_broadcast_if_due(_utc(2026, 10, 13, 11, 5))) == 0
    pb.get_broadcast_targets.assert_not_awaited()
    w.photo.assert_not_awaited()
    pb.notify_admins_named.assert_not_awaited()


def test_a_finished_run_is_not_repeated_within_the_window(monkeypatch):
    w = _wire(monkeypatch)

    asyncio.run(pb.run_broadcast_if_due(MON))
    asyncio.run(pb.run_broadcast_if_due(_utc(2026, 10, 12, 11, 6)))

    assert w.photo.await_count == 3
    assert pb.get_broadcast_targets.await_count == 1
    pb.notify_admins_named.assert_awaited_once()


def test_a_group_it_did_not_reach_is_named_with_telegrams_reason(monkeypatch):
    w = _wire(monkeypatch, targets=[
        _group(-1), _group(-2, "2643", "2643 |GONZALEZ OSVALDO / VAZQUEZ LIZBETH"),
        _group(-3)])
    ok = w.photo.return_value
    w.photo.side_effect = [ok, BadRequest("Not enough rights to send photos"), ok]

    assert asyncio.run(pb.run_broadcast_if_due(MON)) == 2

    assert w.failures.await_args.args[0] == -2
    said = _report(w)
    assert "Posted to 2 group(s)" in said
    assert "1 group(s) did not get it" in said
    assert "GONZALEZ OSVALDO" in said and "-2" in said
    assert "Not enough rights to send photos" in said


def test_the_report_goes_to_safety_monitoring_only(monkeypatch):
    _wire(monkeypatch)

    asyncio.run(pb.run_broadcast_if_due(MON))

    assert pb.notify_admins_named.await_args.args[0] == "Safety Monitoring"


def test_a_clean_run_still_reports_its_count(monkeypatch):
    w = _wire(monkeypatch)

    asyncio.run(pb.run_broadcast_if_due(MON))

    said = _report(w)
    assert "Posted to 3 group(s)" in said and "did not get it" not in said


def test_a_run_that_reached_nobody_tells_nobody(monkeypatch):
    """A restart after the run finished finds every group claimed."""
    _wire(monkeypatch, taken={-1, -2, -3})

    asyncio.run(pb.run_broadcast_if_due(MON))

    pb.notify_admins_named.assert_not_awaited()


def test_a_long_list_of_misses_is_cut_short(monkeypatch):
    many = [_group(-i) for i in range(1, pb.REPORT_LIMIT + 6)]
    w = _wire(monkeypatch, targets=many)
    w.photo.side_effect = BadRequest("Chat not found")

    asyncio.run(pb.run_broadcast_if_due(MON))

    said = _report(w)
    assert f"{len(many)} group(s) did not get it" in said
    assert "…and 5 more." in said
    assert len(said) < 4096


def test_a_migrated_group_is_followed_and_reached_next_tick(monkeypatch):
    w = _wire(monkeypatch, targets=[_group(-1)])
    w.photo.side_effect = MigrateToChat(-1009)

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


# ---------- who hears about it ----------

def _admins(monkeypatch, rows, names):
    monkeypatch.setattr("utils.db.get_admins", AsyncMock(return_value=rows))

    async def get_chat(uid):
        if uid not in names:
            raise BadRequest("Chat not found")
        return SimpleNamespace(full_name=names[uid])

    import loader
    monkeypatch.setattr(loader.bot, "get_chat", get_chat)
    sent = AsyncMock()
    monkeypatch.setattr(loader.bot, "send_message", sent)
    return sent


def test_the_named_admin_gets_it_and_nobody_else(monkeypatch):
    owner = next(iter(admins.SUPER_ADMIN_IDS), 8678782589)
    monkeypatch.setattr(admins, "SUPER_ADMIN_IDS", frozenset({owner}))
    sent = _admins(monkeypatch,
                   rows=[{"user_id": owner, "is_super_admin": True},
                         {"user_id": 11, "is_super_admin": False},
                         {"user_id": 12, "is_super_admin": False}],
                   names={owner: "Safety Monitoring",  # even so: never the owner
                          11: "safety  monitoring", 12: "Brandon"})

    reached = asyncio.run(admins.notify_admins_named("Safety Monitoring", "hi"))

    assert reached == 1
    assert [c.args[0] for c in sent.await_args_list] == [11]


def test_an_admin_the_bot_cannot_read_is_skipped_not_raised(monkeypatch):
    monkeypatch.setattr(admins, "SUPER_ADMIN_IDS", frozenset())
    sent = _admins(monkeypatch,
                   rows=[{"user_id": 11, "is_super_admin": False}], names={})

    assert asyncio.run(admins.notify_admins_named("Safety Monitoring", "hi")) == 0
    sent.assert_not_awaited()
