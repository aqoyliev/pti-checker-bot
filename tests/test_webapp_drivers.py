"""The panel's driver picking: roster search, add, rename and swap.

The Telegram picker can only show so many members and hides known non-drivers,
so a group whose roster starts with 40 office staff offers nobody to tap. The
panel answers the same question as a search, which is why the roster endpoint
hides nothing -- and why every write here has to be as careful as the picker's
Save about what it records.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from utils.userbot import Member
from webapp import server


class _Req(dict):
    """The slice of aiohttp's Request these handlers actually touch."""

    def __init__(self, match_info=None, body=None):
        super().__init__(admin={"user_id": 1, "is_super_admin": True})
        self.match_info = {k: str(v) for k, v in (match_info or {}).items()}
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _call(handler, **kwargs):
    resp = asyncio.run(handler(_Req(**kwargs)))
    return resp.status, json.loads(resp.body)


def _member(uid, name, username=None, is_bot=False):
    return Member(user_id=uid, name=name, username=username, is_bot=is_bot)


DRIVER = _member(2001, "JACQUES FLEURMOND")
STAFF = _member(1001, "DISPATCH DESK")
BOT = _member(9001, "Some Bot", is_bot=True)


@pytest.fixture
def roster(monkeypatch):
    """A configured userbot returning a fixed roster."""
    def _install(members, configured=True):
        monkeypatch.setattr(server.userbot, "is_configured", lambda: configured)
        monkeypatch.setattr(server.userbot, "list_members",
                            AsyncMock(return_value=list(members)))
    return _install


@pytest.fixture(autouse=True)
def db(monkeypatch):
    """Every DB helper the driver endpoints reach for, stubbed."""
    stubs = {
        "get_group": AsyncMock(return_value={"group_id": -100}),
        "get_drivers": AsyncMock(return_value=[]),
        "get_non_driver_ids": AsyncMock(return_value=set()),
        "add_driver": AsyncMock(return_value=True),
        "unmark_non_drivers": AsyncMock(),
        "set_driver_names": AsyncMock(return_value=1),
        "swap_driver": AsyncMock(return_value=True),
    }
    for name, stub in stubs.items():
        monkeypatch.setattr(server, name, stub)
    return stubs


# --- the roster endpoint -------------------------------------------------

def test_no_session_is_a_reason_not_an_error(roster, db):
    roster([], configured=False)
    status, body = _call(server.api_group_members, match_info={"gid": -100})

    assert status == 200
    assert body["available"] is False
    assert "userbot session" in body["reason"]


def test_an_empty_roster_says_the_account_is_probably_not_in_the_group(roster, db):
    roster([])
    _, body = _call(server.api_group_members, match_info={"gid": -100})

    assert body["available"] is False
    assert "not in" in body["reason"]


def test_bots_are_left_out(roster, db):
    roster([DRIVER, BOT])
    _, body = _call(server.api_group_members, match_info={"gid": -100})

    assert [m["user_id"] for m in body["members"]] == [DRIVER.user_id]


def test_registered_drivers_are_flagged(roster, db):
    db["get_drivers"].return_value = [{"user_id": DRIVER.user_id, "name": "X"}]
    roster([DRIVER, STAFF])
    _, body = _call(server.api_group_members, match_info={"gid": -100})

    flags = {m["user_id"]: m["is_driver"] for m in body["members"]}
    assert flags == {DRIVER.user_id: True, STAFF.user_id: False}


def test_known_non_drivers_are_listed_not_hidden(roster, db):
    # The whole point: hiding them is what leaves the Telegram picker empty.
    # Here they are badged and sorted last, but always findable.
    db["get_non_driver_ids"].return_value = {STAFF.user_id}
    roster([STAFF, DRIVER])
    _, body = _call(server.api_group_members, match_info={"gid": -100})

    assert [m["user_id"] for m in body["members"]] == [DRIVER.user_id, STAFF.user_id]
    assert body["members"][1]["is_non_driver"] is True


def test_an_unknown_group_is_404(roster, db):
    db["get_group"].return_value = None
    roster([DRIVER])
    status, _ = _call(server.api_group_members, match_info={"gid": -100})

    assert status == 404


# --- adding a driver -----------------------------------------------------

def test_adding_clears_a_stale_non_driver_row(db):
    # Being picked as a driver outranks a fleet-wide "not a driver", exactly as
    # it does on the onboarding Save path.
    status, _ = _call(server.api_add_driver, match_info={"gid": -100},
                      body={"user_id": 2001, "name": "JACQUES"})

    assert status == 200
    db["unmark_non_drivers"].assert_awaited_once_with([2001])


def test_adding_someone_already_registered_is_rejected(db):
    db["add_driver"].return_value = False
    status, body = _call(server.api_add_driver, match_info={"gid": -100},
                         body={"user_id": 2001, "name": "JACQUES"})

    assert status == 400
    assert "already a driver" in body["error"]


def test_a_nameless_driver_is_rejected(db):
    status, _ = _call(server.api_add_driver, match_info={"gid": -100},
                      body={"user_id": 2001, "name": "   "})

    assert status == 400
    db["add_driver"].assert_not_awaited()


def test_a_non_numeric_user_id_is_rejected(db):
    status, _ = _call(server.api_add_driver, match_info={"gid": -100},
                      body={"user_id": "not-an-id", "name": "JACQUES"})

    assert status == 400
    db["add_driver"].assert_not_awaited()


# --- renaming ------------------------------------------------------------

def test_renaming_an_unregistered_driver_is_404(db):
    db["set_driver_names"].return_value = 0
    db["get_drivers"].return_value = []
    status, _ = _call(server.api_rename_driver,
                      match_info={"gid": -100, "uid": 2001}, body={"name": "NEW"})

    assert status == 404


def test_renaming_to_the_same_name_is_a_no_op_not_a_failure(db):
    # set_driver_names skips a row whose name already matches, so "0 changed"
    # here means "already said that" -- reporting it as an error would have the
    # admin retyping a name that is already correct.
    db["set_driver_names"].return_value = 0
    db["get_drivers"].return_value = [{"user_id": 2001, "name": "NEW"}]
    status, body = _call(server.api_rename_driver,
                         match_info={"gid": -100, "uid": 2001}, body={"name": "NEW"})

    assert status == 200
    assert body["changed"] is False


def test_an_empty_name_never_reaches_the_database(db):
    status, _ = _call(server.api_rename_driver,
                      match_info={"gid": -100, "uid": 2001}, body={"name": ""})

    assert status == 400
    db["set_driver_names"].assert_not_awaited()


# --- swapping ------------------------------------------------------------

def test_swapping_onto_an_existing_driver_is_refused(db):
    # DELETE old + INSERT new would collapse two drivers into one, leaving the
    # unit silently short a driver.
    db["get_drivers"].return_value = [{"user_id": 2002, "name": "OTHER"}]
    status, body = _call(server.api_replace_driver,
                         match_info={"gid": -100, "uid": 2001},
                         body={"user_id": 2002, "name": "OTHER"})

    assert status == 400
    assert "already a driver" in body["error"]
    db["swap_driver"].assert_not_awaited()


def test_swapping_a_driver_who_is_gone_is_404(db):
    db["swap_driver"].return_value = False
    status, _ = _call(server.api_replace_driver,
                      match_info={"gid": -100, "uid": 2001},
                      body={"user_id": 2002, "name": "NEW"})

    assert status == 404


def test_a_swap_clears_the_incoming_driver_non_driver_row(db):
    status, _ = _call(server.api_replace_driver,
                      match_info={"gid": -100, "uid": 2001},
                      body={"user_id": 2002, "name": "NEW"})

    assert status == 200
    db["swap_driver"].assert_awaited_once_with(-100, 2001, 2002, "NEW")
    db["unmark_non_drivers"].assert_awaited_once_with([2002])


def test_renaming_via_swap_to_the_same_id_is_allowed(db):
    # "Change" onto the same person is how a mis-tap gets corrected without
    # first removing the driver; the guard is about *other* registered drivers.
    db["get_drivers"].return_value = [{"user_id": 2001, "name": "OLD"}]
    status, _ = _call(server.api_replace_driver,
                      match_info={"gid": -100, "uid": 2001},
                      body={"user_id": 2001, "name": "NEW"})

    assert status == 200
