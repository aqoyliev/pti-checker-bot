"""The batch setup script: it decides nothing, and it stops when asked to guess.

Two properties are worth pinning. Preview must write nothing -- it is the
default, over a live fleet, and a script that "previewed" by configuring
forty groups would be discovered the hard way. And the run must give up once
the phone lookup stops answering: contact import is the most rate-limited call
the fleet's lookup account has, and a refusal is not an answer about a group.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_path = Path(__file__).resolve().parent.parent / "scripts" / "setup_groups.py"
_spec = importlib.util.spec_from_file_location("setup_groups", _path)
setup_groups = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = setup_groups
_spec.loader.exec_module(setup_groups)

GROUP = {"group_id": -100123, "title": "UNIT 1216 SMITH"}
PLAN = SimpleNamespace(unit="1216", drivers=[(1, "A"), (2, "B")])
ROSTER = [SimpleNamespace(user_id=1, label="A", is_bot=False),
          SimpleNamespace(user_id=2, label="B", is_bot=False)]


@pytest.fixture
def wired(monkeypatch):
    """Everything outside the script itself: Telegram, the roster, the decision."""
    monkeypatch.setattr(setup_groups.bot, "get_chat",
                        AsyncMock(return_value=SimpleNamespace(title="UNIT 1216 X")))
    monkeypatch.setattr(setup_groups.userbot, "list_members",
                        AsyncMock(return_value=list(ROSTER)))
    monkeypatch.setattr(setup_groups.userbot, "get_description",
                        AsyncMock(return_value="UNIT 1216\n786-488-2619 / 561-674-7866"))
    stubs = {
        "_try_auto_config": AsyncMock(return_value=(PLAN, "")),
        "_apply_auto_config": AsyncMock(return_value="notice"),
    }
    for name, mock in stubs.items():
        monkeypatch.setattr(setup_groups, name, mock)
    return stubs


def test_preview_writes_nothing(wired):
    outcome, detail = asyncio.run(setup_groups._setup_one(GROUP, apply=False))

    assert outcome == "would-configure"
    assert "1216" in detail
    wired["_apply_auto_config"].assert_not_awaited()


def test_applying_configures_the_group_with_the_full_roster(wired):
    outcome, _ = asyncio.run(setup_groups._setup_one(GROUP, apply=True))

    assert outcome == "configured"
    args = wired["_apply_auto_config"].await_args.args
    assert args[0] == -100123 and args[1] is PLAN
    # The whole roster goes through, not a keyboard-sized slice of it: it is
    # what the non-driver sweep is judged against.
    assert args[2] == ROSTER


def test_a_declined_group_is_reported_not_written(wired):
    wired["_try_auto_config"].return_value = (None, "+15616747866 matched no account")

    outcome, detail = asyncio.run(setup_groups._setup_one(GROUP, apply=True))

    assert outcome == "declined" and "matched no account" in detail
    wired["_apply_auto_config"].assert_not_awaited()


def test_an_unreadable_roster_costs_no_lookup(wired, monkeypatch):
    """Two drivers who are not visible read as "not in this group", which would
    decline anyway -- after spending two contact imports to find out."""
    monkeypatch.setattr(setup_groups.userbot, "list_members", AsyncMock(return_value=[]))

    outcome, _ = asyncio.run(setup_groups._setup_one(GROUP, apply=True))

    assert outcome == "no-roster"
    wired["_try_auto_config"].assert_not_awaited()


def test_a_group_the_bot_cannot_reach_is_not_a_crash(wired, monkeypatch):
    monkeypatch.setattr(setup_groups.bot, "get_chat",
                        AsyncMock(side_effect=RuntimeError("chat not found")))

    outcome, detail = asyncio.run(setup_groups._setup_one(GROUP, apply=True))

    assert outcome == "unreachable" and "chat not found" in detail


def _run(monkeypatch, outcomes):
    """Drive `run` over len(outcomes) groups, one canned outcome each."""
    groups = [{"group_id": -i, "title": f"UNIT {i}"} for i in range(1, len(outcomes) + 1)]
    monkeypatch.setattr(setup_groups.db, "init_db", AsyncMock())
    monkeypatch.setattr(setup_groups.db, "get_unconfigured_groups",
                        AsyncMock(return_value=groups))
    seen = []

    async def fake(group, apply):
        seen.append(group["group_id"])
        return outcomes[len(seen) - 1]

    monkeypatch.setattr(setup_groups, "_setup_one", fake)
    asyncio.run(setup_groups.run(apply=True, only=[], sleep=0, limit=None))
    return seen


def test_it_stops_once_the_lookup_stops_answering(monkeypatch):
    refused = ("declined", f"{setup_groups.LOOKUP_UNAVAILABLE} (contact-import limited)")
    seen = _run(monkeypatch, [refused] * 10)

    assert len(seen) == setup_groups.LOOKUP_FAILURE_LIMIT


def test_ordinary_declines_do_not_stop_the_run(monkeypatch):
    """"That number matches nobody" is an answer about one group, and says
    nothing about the next one."""
    seen = _run(monkeypatch, [("declined", "no phone numbers in the About text")] * 6)

    assert len(seen) == 6


def test_the_failure_streak_resets_on_a_group_that_worked(monkeypatch):
    refused = ("declined", f"{setup_groups.LOOKUP_UNAVAILABLE} (flood wait)")
    ok = ("configured", "unit 1216")
    seen = _run(monkeypatch, [refused, refused, ok, refused, refused, ok])

    assert len(seen) == 6
