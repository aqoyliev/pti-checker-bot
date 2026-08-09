"""The active-units list: parsing a pasted blob, and when to ask for a new one.

Unit numbers are strings, not ints: "001" and "0822" are real units in this
fleet, and int() would turn them into 1 and 822 -- silently failing to match the
group they belong to.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from handlers.admin import units


def test_parses_space_separated():
    assert units.parse_units("1332 1303 728453") == ["1332", "1303", "728453"]


def test_parses_commas_newlines_and_tabs():
    assert units.parse_units("1332,1303\n728453\t2001") == [
        "1332", "1303", "728453", "2001"]


def test_keeps_leading_zeros():
    # "001" must not become "1" -- it is a distinct unit in the fleet list.
    assert units.parse_units("001 0822 1136") == ["001", "0822", "1136"]


def test_ignores_surrounding_prose():
    assert units.parse_units("active: 1332, 1303 (as of Monday)") == ["1332", "1303"]


def test_empty_input_yields_nothing():
    assert units.parse_units("") == []
    assert units.parse_units(None) == []


def test_the_real_fleet_list_parses_to_165_units():
    # The list supplied on 2026-08-07, in the layout it arrived in.
    blob = """
    1332 1303 728453 218765 1275 2001 1310 1284 1283 488093 1172 1209 2003 1256
    1302 1216 1243 1280 709039 1344 1249 1250 1348 1282 2022 1307 1312 1291 1285
    """
    parsed = units.parse_units(blob)
    assert len(parsed) == 29
    assert parsed[0] == "1332"
    assert len(set(parsed)) == len(parsed)


def _run_ask(monkeypatch, updated, asked, admin_ids=(7564871221,)):
    monkeypatch.setattr(units, "_ADMIN_IDS", list(admin_ids))
    monkeypatch.setattr(units, "active_units_updated_at", AsyncMock(return_value=updated))
    monkeypatch.setattr(units, "get_setting",
                        AsyncMock(return_value=asked.isoformat() if asked else None))
    monkeypatch.setattr(units, "set_setting", AsyncMock())
    monkeypatch.setattr(units.bot, "send_message", AsyncMock(return_value=True))
    return asyncio.run(units.ask_for_units_if_stale())


def test_asks_when_no_list_has_ever_been_supplied(monkeypatch):
    assert _run_ask(monkeypatch, updated=None, asked=None) is True


def test_does_not_ask_while_the_list_is_fresh(monkeypatch):
    recent = datetime.utcnow() - timedelta(days=2)
    assert _run_ask(monkeypatch, updated=recent, asked=None) is False


def test_asks_once_the_list_is_a_week_old(monkeypatch):
    stale = datetime.utcnow() - timedelta(days=8)
    assert _run_ask(monkeypatch, updated=stale, asked=None) is True


def test_does_not_re_ask_every_cycle_while_unanswered(monkeypatch):
    # List still stale and nobody replied, but we asked an hour ago: stay quiet.
    stale = datetime.utcnow() - timedelta(days=30)
    just_asked = datetime.utcnow() - timedelta(hours=1)
    assert _run_ask(monkeypatch, updated=stale, asked=just_asked) is False


def test_asks_again_a_week_after_an_ignored_ask(monkeypatch):
    stale = datetime.utcnow() - timedelta(days=30)
    old_ask = datetime.utcnow() - timedelta(days=8)
    assert _run_ask(monkeypatch, updated=stale, asked=old_ask) is True


def test_reports_no_ask_when_no_admin_is_reachable(monkeypatch):
    monkeypatch.setattr(units, "_ADMIN_IDS", [])
    monkeypatch.setattr(units, "active_units_updated_at", AsyncMock(return_value=None))
    monkeypatch.setattr(units, "get_setting", AsyncMock(return_value=None))
    monkeypatch.setattr(units, "set_setting", AsyncMock())
    assert asyncio.run(units.ask_for_units_if_stale()) is False
