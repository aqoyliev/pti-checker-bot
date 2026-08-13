"""Resolving an admin id to a name.

The admins table stores ids only, so the panel asks Telegram. That call can fail
-- an admin who has never opened a DM with the bot has no profile to fetch --
and the panel must degrade to a bare id rather than error, since failing here
would take out the whole Admins tab over a cosmetic label.
"""
import asyncio
from types import SimpleNamespace

import pytest

from webapp import server


class _Bot:
    """Stands in for loader.bot: answers for known ids, raises for the rest."""

    def __init__(self, users: dict):
        self.users = users
        self.calls = 0

    async def get_chat(self, uid):
        self.calls += 1
        if uid not in self.users:
            raise RuntimeError("Bad Request: chat not found")
        return self.users[uid]


def _user(first=None, last=None, username=None):
    return SimpleNamespace(first_name=first, last_name=last, username=username)


@pytest.fixture(autouse=True)
def clear_cache():
    server._user_cache.clear()
    yield
    server._user_cache.clear()


def _card(monkeypatch, users, uid):
    monkeypatch.setattr(server, "bot", _Bot(users))
    return asyncio.run(server._user_card(uid))


def test_first_and_last_name_are_joined(monkeypatch):
    card = _card(monkeypatch, {7: _user("Rick", "Smith", "rick")}, 7)

    assert card == {"name": "Rick Smith", "username": "rick"}


def test_a_first_name_alone_is_enough(monkeypatch):
    assert _card(monkeypatch, {7: _user("Rick")}, 7)["name"] == "Rick"


def test_a_nameless_profile_yields_none_not_an_empty_string(monkeypatch):
    # The UI falls back on `a.name || ...`, so "" and None behave the same --
    # but None is what the field means, and it keeps the JSON honest.
    assert _card(monkeypatch, {7: _user(username="rick")}, 7)["name"] is None


def test_an_unresolvable_admin_degrades_to_no_name(monkeypatch):
    # Never raises: an admin the bot has no DM with still has to be listed,
    # and still has to be removable.
    assert _card(monkeypatch, {}, 7) == {"name": None, "username": None}


def test_the_result_is_cached(monkeypatch):
    bot = _Bot({7: _user("Rick")})
    monkeypatch.setattr(server, "bot", bot)

    asyncio.run(server._user_card(7))
    asyncio.run(server._user_card(7))

    assert bot.calls == 1


def test_a_failure_is_cached_too(monkeypatch):
    # Otherwise every render of the tab re-asks Telegram for an id that will
    # never resolve, once per admin.
    bot = _Bot({})
    monkeypatch.setattr(server, "bot", bot)

    asyncio.run(server._user_card(7))
    asyncio.run(server._user_card(7))

    assert bot.calls == 1


def test_cards_are_fetched_for_every_id(monkeypatch):
    monkeypatch.setattr(server, "bot", _Bot({
        1: _user("Rick", username="rick"),
        2: _user("Sam"),
    }))

    cards = asyncio.run(server._user_cards([1, 2, 3]))

    assert cards[1]["username"] == "rick"
    assert cards[2] == {"name": "Sam", "username": None}
    assert cards[3] == {"name": None, "username": None}
