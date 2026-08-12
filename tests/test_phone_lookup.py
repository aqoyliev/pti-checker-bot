"""Phone -> account lookup: number parsing, contact cleanup, and the boundary
between "no such account" and "the lookup itself failed".

Confusing those two is the failure that matters. A contact-import limit returns
the number in retry_contacts forever; reporting that as "not on Telegram" would
have an admin chasing a driver who is in fact perfectly reachable.
"""
import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils import phone_lookup, userbot
from utils.phone_lookup import LookupUnavailable, extract_phones, normalize_phone


# ---------- normalize_phone ----------

def test_bare_ten_digit_number_gets_the_us_country_code():
    # A number typed off a driver list has no country code, and Telegram
    # resolves nothing for one — silently, which reads like "not registered".
    assert normalize_phone("786-488-2619") == "+17864882619"
    assert normalize_phone("(561) 674 7866") == "+15616747866"


def test_explicit_plus_is_trusted_as_international():
    assert normalize_phone("+44 7700 900123") == "+447700900123"
    assert normalize_phone("+1 786 488 2619") == "+17864882619"


def test_eleven_digit_us_number_keeps_its_country_code():
    assert normalize_phone("1-786-488-2619") == "+17864882619"


def test_non_numbers_are_rejected_rather_than_guessed():
    for junk in ("", "   ", "hello", "1225", "12345"):
        assert normalize_phone(junk) is None


# ---------- extract_phones ----------

def test_finds_numbers_in_a_group_description():
    about = ("UNIT 1225\nDriver: Mohamed Magan 786-488-2619\n"
             "Co-driver: Noor Dubat (561) 674-7866")
    assert extract_phones(about) == ["+17864882619", "+15616747866"]


def test_ignores_the_numbers_a_description_is_full_of():
    # Unit, trailer and year are all digits, and reading one as a phone number
    # would send an admin looking up an account that cannot exist.
    about = "UNIT 1216 // TRAILER 53812 // Established 2019 // rate $2.75"
    assert extract_phones(about) == []


def test_the_same_number_written_twice_is_one_result():
    assert extract_phones("call 786-488-2619 or +1 786 488 2619") == ["+17864882619"]


def test_empty_description_is_no_numbers():
    assert extract_phones("") == []
    assert extract_phones(None) == []


# ---------- lookup() ----------

class _FakeClient:
    """Records the requests it is sent and replays scripted import results."""

    def __init__(self, scripted, profile=None):
        self.scripted = list(scripted)
        self.requests = []
        self.profile = profile
        self.entity_reads = []

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name == "DeleteContactsRequest":
            return SimpleNamespace()
        return self.scripted.pop(0)

    async def get_entity(self, user_id):
        self.entity_reads.append((user_id, len(self.requests)))
        if self.profile is None:
            raise ValueError("no such entity")
        return self.profile


def _imported(client_id, user_id):
    return SimpleNamespace(client_id=client_id, user_id=user_id)


def _user(user_id, first="Noor", last="Dubat", username="noor", bot=False):
    return SimpleNamespace(id=user_id, first_name=first, last_name=last,
                           username=username, bot=bot, phone="15616747866")


def _result(imported=(), users=(), retry=()):
    return SimpleNamespace(imported=list(imported), users=list(users),
                           retry_contacts=list(retry), popular_invites=[])


def _install(monkeypatch, client):
    monkeypatch.setattr(phone_lookup, "_get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(phone_lookup.asyncio, "sleep", AsyncMock())


def test_resolved_number_returns_the_account(monkeypatch):
    client = _FakeClient([_result(imported=[_imported(0, 6066541941)],
                                  users=[_user(6066541941)])])
    _install(monkeypatch, client)

    out = asyncio.run(phone_lookup.lookup(["561-674-7866"]))

    m = out["561-674-7866"]
    assert (m.user_id, m.name, m.username) == (6066541941, "Noor Dubat", "noor")


def test_the_placeholder_contact_name_is_never_reported_as_a_person(monkeypatch):
    """Telegram reports a saved contact under *your* name for them.

    While the contact exists, importContacts hands back a User called "lookup"
    -- the label this module imports under -- so a naive read renames every
    driver in the fleet to "lookup". The real name only appears once the
    contact is gone, so the profile is re-read after deletion.
    """
    imported_user = _user(487701649, first=phone_lookup._CONTACT_NAME, last="",
                          username="akbarqoyliev")
    real_profile = _user(487701649, first="Akbar", last="", username="akbarqoyliev")
    client = _FakeClient([_result(imported=[_imported(0, 487701649)],
                                  users=[imported_user])], profile=real_profile)
    _install(monkeypatch, client)

    m = asyncio.run(phone_lookup.lookup(["+998970078373"]))["+998970078373"]

    assert m.name == "Akbar"
    # The re-read must happen after the delete, or it returns "lookup" again.
    (_, requests_before_read), = client.entity_reads
    assert [type(r).__name__ for r in client.requests[:requests_before_read]][-1] \
        == "DeleteContactsRequest"


def test_a_failed_profile_reread_drops_the_name_rather_than_lying(monkeypatch):
    imported_user = _user(487701649, first=phone_lookup._CONTACT_NAME, last="",
                          username="akbarqoyliev")
    client = _FakeClient([_result(imported=[_imported(0, 487701649)],
                                  users=[imported_user])], profile=None)
    _install(monkeypatch, client)

    m = asyncio.run(phone_lookup.lookup(["+998970078373"]))["+998970078373"]

    assert m.name == ""
    assert (m.user_id, m.username) == (487701649, "akbarqoyliev")


def test_the_imported_contact_is_always_deleted_again(monkeypatch):
    """The write must be undone, or the lookup account slowly fills with
    every driver the fleet ever asked about."""
    client = _FakeClient([_result(imported=[_imported(0, 6066541941)],
                                  users=[_user(6066541941)])])
    _install(monkeypatch, client)

    asyncio.run(phone_lookup.lookup(["561-674-7866"]))

    assert [type(r).__name__ for r in client.requests] == [
        "ImportContactsRequest", "DeleteContactsRequest"]


def test_answered_but_unknown_number_is_a_plain_no_match(monkeypatch):
    # Neither imported nor flagged for retry: Telegram has answered, and the
    # answer is "nobody visible".
    client = _FakeClient([_result()])
    _install(monkeypatch, client)

    assert asyncio.run(phone_lookup.lookup(["786-488-2619"])) == {"786-488-2619": None}


def test_endless_retry_raises_instead_of_reporting_no_match(monkeypatch):
    # This is the contact-import limit: retry_contacts every time, nothing ever
    # imported. Reporting None here would blame the driver for an account
    # problem on our side.
    client = _FakeClient([_result(retry=[0]) for _ in range(3)])
    _install(monkeypatch, client)

    with pytest.raises(LookupUnavailable):
        asyncio.run(phone_lookup.lookup(["786-488-2619"]))


def test_retry_then_success_resolves(monkeypatch):
    client = _FakeClient([
        _result(retry=[0]),
        _result(imported=[_imported(0, 6066541941)], users=[_user(6066541941)]),
    ])
    _install(monkeypatch, client)

    out = asyncio.run(phone_lookup.lookup(["561-674-7866"]))

    assert out["561-674-7866"].user_id == 6066541941


def test_one_refused_number_does_not_fail_the_batch(monkeypatch):
    # Something was imported, so the account is not limited; the other number
    # is simply unresolvable and must come back as None, not as an exception.
    client = _FakeClient([
        _result(imported=[_imported(0, 6066541941)], users=[_user(6066541941)],
                retry=[1]),
        _result(retry=[0]),
        _result(retry=[0]),
    ])
    _install(monkeypatch, client)

    out = asyncio.run(phone_lookup.lookup(["561-674-7866", "786-488-2619"]))

    assert out["561-674-7866"].user_id == 6066541941
    assert out["786-488-2619"] is None


def test_unconfigured_session_raises_rather_than_answering(monkeypatch):
    monkeypatch.setattr(phone_lookup, "_get_client", AsyncMock(return_value=None))

    with pytest.raises(LookupUnavailable):
        asyncio.run(phone_lookup.lookup(["786-488-2619"]))


def test_unparseable_input_never_reaches_telegram(monkeypatch):
    client = _FakeClient([])
    _install(monkeypatch, client)

    assert asyncio.run(phone_lookup.lookup(["not a phone"])) == {"not a phone": None}
    assert client.requests == []


# ---------- the read-only rule ----------

def test_the_roster_userbot_stays_read_only():
    """utils/userbot.py must never grow a write.

    Phone lookup is the first thing the project needed that writes to a user
    account, and the obvious place to put it was the module that already has a
    client. Keeping the write out of there is what stops the roster session --
    load-bearing for onboarding -- from being rate-limited or banned.
    """
    source = inspect.getsource(userbot)
    for write in ("ImportContacts", "DeleteContacts", "send_message",
                  "JoinChannel", "LeaveChannel", "EditMessage"):
        assert write not in source, f"utils/userbot.py must stay read-only ({write})"
