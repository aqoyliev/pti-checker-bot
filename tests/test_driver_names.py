"""Reading the fleet's driver names, and pairing them to registered drivers.

Pure: no DB, no Telegram. /fixnames renames drivers fleet-wide from this
decision, and a name put on the wrong user_id reads as authoritative -- so what
it refuses to pair is the point.
"""
import asyncio
from unittest.mock import AsyncMock

from handlers.admin import names as fixnames
from utils.driver_names import match_names_to_drivers, parse_driver_names

ABOUT = ("Name: ZAMA, EMILE / FLEURMOND, JACQUES\n"
         "Phone# 718-864-1154 / 561-667-4276\n"
         "Truck# 1239")


def _d(user_id: int, name: str) -> dict:
    return {"user_id": user_id, "name": name}


# ---------- reading the About text ----------

def test_names_are_read_in_order():
    assert parse_driver_names(ABOUT) == ["Zama Emile", "Fleurmond Jacques"]


def test_one_name_line_per_driver():
    about = "Name: ZAMA, EMILE\nPhone# 718-864-1154\nName: FLEURMOND, JACQUES"
    assert parse_driver_names(about) == ["Zama Emile", "Fleurmond Jacques"]


def test_a_properly_cased_name_is_not_re_cased():
    # .title() would turn McDonald into Mcdonald.
    assert parse_driver_names("Name: Ian McDonald") == ["Ian McDonald"]


def test_a_phone_number_on_a_name_line_is_not_a_name():
    assert parse_driver_names("Driver: 786-488-2619") == []


def test_a_separator_is_required():
    # About text is free prose; without this, a sentence mentioning a name reads
    # as a driver list.
    assert parse_driver_names("the name on the lease is Acme LLC") == []


# ---------- pairing ----------

def test_a_shared_word_pairs_a_name_to_a_driver():
    drivers = [_d(1, "Emile ✈️"), _d(2, "jacques_f")]

    placed, left = match_names_to_drivers(parse_driver_names(ABOUT), drivers)

    assert placed == {1: "Zama Emile", 2: "Fleurmond Jacques"}
    assert left == []


def test_one_name_and_one_driver_needs_no_evidence():
    # The About text belongs to this group; there is only one person it can be
    # about, whatever Telegram calls them.
    placed, left = match_names_to_drivers(["Zama Emile"], [_d(1, "🚛")])

    assert placed == {1: "Zama Emile"} and left == []


def test_an_unrecognisable_telegram_name_is_left_alone():
    # Two candidates and nothing to choose between them: a wrong name on a
    # user_id is worse than the handle that's there now.
    placed, left = match_names_to_drivers(parse_driver_names(ABOUT),
                                          [_d(1, "🚛"), _d(2, "Trucker")])

    assert placed == {}
    assert left == ["Zama Emile", "Fleurmond Jacques"]


def test_two_drivers_sharing_a_surname_are_not_paired():
    names = ["Mohamed Ali", "Mohamed Hassan"]
    drivers = [_d(1, "Mohamed"), _d(2, "Mohamed A")]

    placed, left = match_names_to_drivers(names, drivers)

    assert placed == {}
    assert left == names


def test_a_proven_pair_survives_an_unprovable_one():
    # One name matching one driver is evidence on its own; the other name being
    # unmatchable doesn't taint it.
    placed, left = match_names_to_drivers(parse_driver_names(ABOUT),
                                          [_d(1, "Emile ✈️"), _d(2, "🚛")])

    assert placed == {1: "Zama Emile"}
    assert left == ["Fleurmond Jacques"]


def test_initials_are_too_short_to_pair_on():
    placed, _ = match_names_to_drivers(["Ali Jr", "Omar Jr"],
                                       [_d(1, "Jr"), _d(2, "JR")])
    assert placed == {}


def test_nothing_to_do_without_names_or_drivers():
    assert match_names_to_drivers([], [_d(1, "Emile")]) == ({}, [])
    assert match_names_to_drivers(["Zama Emile"], []) == ({}, ["Zama Emile"])


# ---------- the fleet scan ----------

def _wire(monkeypatch, groups, drivers, about):
    monkeypatch.setattr(fixnames, "get_all_groups", AsyncMock(return_value=groups))
    monkeypatch.setattr(fixnames, "get_all_drivers_by_group",
                        AsyncMock(return_value=drivers))
    monkeypatch.setattr(fixnames.userbot, "get_description",
                        AsyncMock(side_effect=lambda gid: about.get(gid, "")))
    monkeypatch.setattr(fixnames, "_FETCH_DELAY", 0)


def _group(gid, unit, active=True):
    return {"group_id": gid, "unit_number": unit, "is_active": active}


def test_the_scan_finds_renames_and_writes_nothing(monkeypatch):
    _wire(monkeypatch,
          groups=[_group(-1, "1239")],
          drivers={-1: [_d(1, "Emile ✈️"), _d(2, "jacques_f")]},
          about={-1: ABOUT})

    result = asyncio.run(fixnames.scan_fleet())

    assert [(c["user_id"], c["old"], c["new"]) for c in result["changes"]] == [
        (1, "Emile ✈️", "Zama Emile"),
        (2, "jacques_f", "Fleurmond Jacques"),
    ]
    assert result["scanned"] == 1


def test_a_name_already_stored_is_not_a_change(monkeypatch):
    _wire(monkeypatch,
          groups=[_group(-1, "1239")],
          drivers={-1: [_d(1, "Zama Emile"), _d(2, "Fleurmond Jacques")]},
          about={-1: ABOUT})

    assert asyncio.run(fixnames.scan_fleet())["changes"] == []


def test_inactive_and_unconfigured_groups_are_not_scanned(monkeypatch):
    _wire(monkeypatch,
          groups=[_group(-1, "1239", active=False), _group(-2, None)],
          drivers={-1: [_d(1, "Emile ✈️")], -2: []},
          about={-1: ABOUT, -2: ABOUT})

    result = asyncio.run(fixnames.scan_fleet())

    assert result["changes"] == [] and result["scanned"] == 0


def test_an_unreadable_about_text_is_counted_not_guessed(monkeypatch):
    # get_description returns "" when the userbot can't read the group. That is
    # not evidence about anyone's name.
    _wire(monkeypatch,
          groups=[_group(-1, "1239")],
          drivers={-1: [_d(1, "Emile ✈️")]},
          about={})

    result = asyncio.run(fixnames.scan_fleet())

    assert result["changes"] == [] and result["no_names"] == 1
