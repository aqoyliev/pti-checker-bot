"""A chat the bot is in but has no `groups` row is invisible to everything.

The nag, the daily title sweep and the web panel all read that table, so a
missing row means nothing can ask for the group's setup however busy it gets --
while `/check` goes on telling its drivers the fleet admins have been asked.
Found on Gurman on 2026-10-01: one chat with 911 messages over 45 days.
"""
import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from handlers.groups import setup_nag
from utils import db


class _Stop(Exception):
    """Breaks the loop out of its sleep so one iteration can be observed."""


def test_every_unrecorded_chat_gets_a_row(monkeypatch):
    monkeypatch.setattr(setup_nag, "get_chats_without_a_record",
                        AsyncMock(return_value=[-100, -200]))
    upsert = AsyncMock()
    monkeypatch.setattr(setup_nag, "upsert_group", upsert)

    assert asyncio.run(setup_nag._register_unrecorded_chats()) == 2
    assert [c.args[0] for c in upsert.await_args_list] == [-100, -200]


def test_nothing_is_written_when_every_chat_is_known(monkeypatch):
    """The ordinary case, once a minute, forever."""
    monkeypatch.setattr(setup_nag, "get_chats_without_a_record",
                        AsyncMock(return_value=[]))
    upsert = AsyncMock()
    monkeypatch.setattr(setup_nag, "upsert_group", upsert)

    assert asyncio.run(setup_nag._register_unrecorded_chats()) == 0
    upsert.assert_not_awaited()


def test_the_query_looks_for_no_row_at_all(monkeypatch):
    """Not "unconfigured" -- *absent*.

    A chat the bot has really been removed from is registered once, fails the
    nag's own prompt and is retired by the unreachable strikes. It keeps its
    row through that, so an anti-join can never resurrect it; a query that
    looked for unconfigured-and-inactive groups would re-prompt it every
    minute for as long as its message buckets survive.
    """
    sql = inspect.getsource(db.get_chats_without_a_record)
    assert "LEFT JOIN groups" in sql
    assert "g.group_id IS NULL" in sql
    # Bounded: a fleet-wide gap is handed over several passes, not all at once.
    assert "LIMIT $1" in sql


def test_a_failed_registration_still_leaves_the_prompts_running(monkeypatch):
    """Guarded separately. A chat that cannot be registered must not cost the
    prompt for every group that already has a row."""
    monkeypatch.setattr(setup_nag, "_register_unrecorded_chats",
                        AsyncMock(side_effect=RuntimeError("boom")))
    nag_pass = AsyncMock()
    monkeypatch.setattr(setup_nag, "_run_setup_nag_pass", nag_pass)

    async def _stop(_seconds):
        raise _Stop

    monkeypatch.setattr(setup_nag.asyncio, "sleep", _stop)

    with pytest.raises(_Stop):
        asyncio.run(setup_nag.setup_nag_loop())

    nag_pass.assert_awaited_once()


def test_registering_is_all_it_does(monkeypatch):
    """No prompt, no post, no roster read of its own.

    The row is what makes the group visible; the nag pass then asks about it
    the same way it asks about any group whose join-time prompt reached
    nobody. A second notification here would be a second thing to get wrong,
    and an MTProto roster read per pass would spend the lookup budget that
    onboarding depends on.
    """
    monkeypatch.setattr(setup_nag, "get_chats_without_a_record",
                        AsyncMock(return_value=[-100]))
    monkeypatch.setattr(setup_nag, "upsert_group", AsyncMock())
    sent = AsyncMock()
    monkeypatch.setattr(setup_nag.bot, "send_message", sent)
    onboarding = AsyncMock()
    monkeypatch.setattr(setup_nag, "start_onboarding", onboarding)

    asyncio.run(setup_nag._register_unrecorded_chats())

    sent.assert_not_awaited()
    onboarding.assert_not_awaited()
