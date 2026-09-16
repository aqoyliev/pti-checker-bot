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


# ---------- the suggestion report ----------

def test_names_come_out_of_the_title_when_the_about_text_has_none():
    """Half these groups write the drivers into the title and nowhere else --
    no label, no phone line under them, nothing a parser can hold on to."""
    assert setup_groups._names_from_title(
        "2643 |GONZALEZ OSVALDO / VAZQUEZ LIZBETH", "2643"
    ) == ["GONZALEZ OSVALDO", "VAZQUEZ LIZBETH"]
    assert setup_groups._names_from_title(
        "1164 - SINEUS RUDOLPH / KNIGHT, DAMON", "1164"
    ) == ["SINEUS RUDOLPH", "KNIGHT DAMON"]
    # "UNIT" labels the number, "( LO )" labels the lease, neither is a name.
    assert setup_groups._names_from_title(
        "UNIT 2644 MOLO, DAVID / JOSEPH , NICKEL ( LO )", "2644"
    ) == ["MOLO DAVID", "JOSEPH NICKEL"]
    # Some titles carry the phone numbers too; a word with a digit in it is not
    # part of anybody's name.
    assert setup_groups._names_from_title(
        "212582 - GAITER ERIC 772-489-1955 ; MILLER MURTON 772-626-4417", "212582"
    ) == ["GAITER ERIC", "MILLER MURTON"]


def test_a_title_with_no_names_yields_none():
    assert setup_groups._names_from_title("UNIT 2002", "2002") == []


def _suggest(monkeypatch, *, title, about, roster, hidden=frozenset()):
    monkeypatch.setattr(setup_groups.bot, "get_chat",
                        AsyncMock(return_value=SimpleNamespace(title=title)))
    monkeypatch.setattr(setup_groups.userbot, "list_members",
                        AsyncMock(return_value=list(roster)))
    monkeypatch.setattr(setup_groups.userbot, "get_description",
                        AsyncMock(return_value=about))
    return asyncio.run(setup_groups._suggest_one({"group_id": -100123}, set(hidden)))


def test_a_proven_pair_is_reported_with_the_word_that_proved_it(monkeypatch, wired):
    roster = [SimpleNamespace(user_id=11, label="Osvaldo Gonzalez", is_bot=False),
              SimpleNamespace(user_id=12, label="Lizbeth V", is_bot=False),
              SimpleNamespace(user_id=13, label="Dispatch Ana", is_bot=False)]

    pairs, lines = _suggest(monkeypatch, title="2643 GONZALEZ OSVALDO / LIZBETH",
                            about="", roster=roster)

    assert pairs == 2
    report = "\n".join(lines)
    assert "Osvaldo Gonzalez (11)" in report and "Lizbeth V (12)" in report
    assert "gonzalez" in report          # the shared word is shown, not implied
    assert "Dispatch Ana" not in report
    # It reads and reports. Nothing here may write or spend a phone lookup.
    wired["_try_auto_config"].assert_not_awaited()
    wired["_apply_auto_config"].assert_not_awaited()


def test_two_members_sharing_a_surname_are_left_to_the_person(monkeypatch, wired):
    """The rule /fixnames uses: a pair that could be either is not a pair."""
    roster = [SimpleNamespace(user_id=11, label="Jama Mohamed", is_bot=False),
              SimpleNamespace(user_id=12, label="Mohamed Jama", is_bot=False)]

    pairs, lines = _suggest(monkeypatch, title="2629 JAMA MOHAMMED / JAMA MOHAMED",
                            about="", roster=roster)

    assert pairs == 0
    report = "\n".join(lines)
    assert "2 possible" in report
    assert "Jama Mohamed (11)" in report and "Mohamed Jama (12)" in report


def test_the_about_text_wins_over_the_title(monkeypatch, wired):
    roster = [SimpleNamespace(user_id=11, label="Anel B", is_bot=False)]

    _, lines = _suggest(monkeypatch, title="215237 SOMEBODY ELSE",
                        about="Name: BEAUCICOT ANEL\nPhone# 407-785-9127",
                        roster=roster)

    assert "names read from the About text" in "\n".join(lines)


def test_a_driver_the_sweep_has_hidden_says_so(monkeypatch, wired):
    """After a fleet-wide setup the person to pick is often on the non-driver
    list, and the picker hides them until someone taps Show hidden."""
    roster = [SimpleNamespace(user_id=11, label="Osvaldo Gonzalez", is_bot=False)]

    _, lines = _suggest(monkeypatch, title="2643 GONZALEZ OSVALDO", about="",
                        roster=roster, hidden={11})

    assert "Show hidden" in "\n".join(lines)


def test_a_group_with_no_roster_is_reported_not_guessed(monkeypatch, wired):
    pairs, lines = _suggest(monkeypatch, title="OVQAT GRUPPA", about="", roster=[])

    assert pairs == 0
    assert "no member list" in "\n".join(lines)
