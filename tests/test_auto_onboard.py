"""Configuring a group from the phone numbers in its About text.

The whole point of this path is that nobody looks at it, so the bar for taking
it has to be high: a wrong auto-save misattributes every later inspection in
the group and there is no admin in the loop to notice. These tests pin the
conditions under which it declines and hands the group back to the picker.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.admin import onboard
from utils.auto_onboard import plan_auto_config
from utils.phone_lookup import LookupUnavailable, Match
from utils.userbot import Member


def _member(user_id, name="Driver", is_bot=False):
    return SimpleNamespace(user_id=user_id, label=name, is_bot=is_bot)


def _match(user_id):
    return SimpleNamespace(user_id=user_id)


PHONES = ["+17864882619", "+15616747866"]
MEMBERS = [_member(8063167928, "M Mgn"), _member(6066541941, "Noor"),
           _member(555, "Dispatch")]
RESOLVED = {PHONES[0]: _match(8063167928), PHONES[1]: _match(6066541941)}


def test_configures_when_both_numbers_are_members():
    plan, reason = plan_auto_config("1225", PHONES, RESOLVED, MEMBERS)

    assert reason == ""
    assert plan.unit == "1225"
    assert plan.drivers == [(8063167928, "M Mgn"), (6066541941, "Noor")]
    # The notice has to say where each driver came from, or an admin checking a
    # wrong pick has no way to tell which number produced it.
    assert plan.sources[8063167928] == PHONES[0]


def test_no_unit_means_ask():
    # _checked_guess already returns None for a unit that is not on the active
    # list, so "no unit" also covers "the parsed unit is retired".
    plan, reason = plan_auto_config(None, PHONES, RESOLVED, MEMBERS)

    assert plan is None
    assert "unit" in reason


def test_a_number_that_resolves_to_nobody_means_ask():
    resolved = {PHONES[0]: _match(8063167928), PHONES[1]: None}

    plan, reason = plan_auto_config("1225", PHONES, resolved, MEMBERS)

    assert plan is None
    assert PHONES[1] in reason


def test_an_account_outside_the_group_means_ask():
    """A resolved account that is not in the chat cannot post a PTI.

    Registering it would create a driver who is permanently overdue and can
    never clear it -- worse than not configuring the group at all.
    """
    plan, reason = plan_auto_config("1225", PHONES, RESOLVED,
                                    [_member(8063167928, "M Mgn")])

    assert plan is None
    assert "not in this group" in reason


def test_three_numbers_means_ask():
    # One of them belongs to dispatch or the owner, and picking which two are
    # drivers is exactly the guess this path must not make.
    phones = PHONES + ["+13055551234"]
    resolved = dict(RESOLVED, **{phones[2]: _match(555)})

    plan, reason = plan_auto_config("1225", phones, resolved, MEMBERS)

    assert plan is None
    assert "too many" in reason


def test_no_numbers_means_ask():
    plan, reason = plan_auto_config("1225", [], {}, MEMBERS)

    assert plan is None
    assert "no phone numbers" in reason


def test_two_numbers_on_one_account_means_ask():
    # Same person listed twice leaves the second driver unregistered while
    # looking like a complete configuration.
    resolved = {p: _match(8063167928) for p in PHONES}

    plan, reason = plan_auto_config("1225", PHONES, resolved, MEMBERS)

    assert plan is None
    assert "same account" in reason


def test_a_bot_is_never_a_driver():
    members = [_member(8063167928, "M Mgn"), _member(6066541941, "QM", is_bot=True)]

    plan, reason = plan_auto_config("1225", PHONES, RESOLVED, members)

    assert plan is None
    assert "bot" in reason


# ---------- the fleet's names, not Telegram's ----------

ABOUT_NAMED = ("Name: ZAMA, EMILE / FLEURMOND, JACQUES\n"
               "Phone# 718-864-1154 / 561-667-4276\n"
               "Truck# 1239")


def test_the_fleet_name_is_what_gets_stored():
    plan, reason = plan_auto_config("1225", PHONES, RESOLVED, MEMBERS,
                                    ["Zama Emile", "Fleurmond Jacques"])

    assert reason == ""
    assert plan.drivers == [(8063167928, "Zama Emile"),
                            (6066541941, "Fleurmond Jacques")]
    # The Telegram name is kept for the notice, so a swapped pair is visible.
    assert plan.tg_labels[8063167928] == "M Mgn"


def test_a_name_count_that_does_not_match_falls_back_to_telegram():
    # Names pair with numbers by position; one name and two numbers is not a
    # pairing, and guessing whose it is misattributes a driver.
    plan, _ = plan_auto_config("1225", PHONES, RESOLVED, MEMBERS, ["Zama Emile"])

    assert plan.drivers == [(8063167928, "M Mgn"), (6066541941, "Noor")]


def test_no_names_at_all_still_configures_the_group():
    # The name is a label; the user_id is the load-bearing part. Missing names
    # must never cost an otherwise-clean automatic setup.
    plan, reason = plan_auto_config("1225", PHONES, RESOLVED, MEMBERS, [])

    assert reason == ""
    assert plan.drivers == [(8063167928, "M Mgn"), (6066541941, "Noor")]


# ---------- start_onboarding: the whole path ----------

ABOUT = "UNIT 1216\nDriver 786-488-2619\nCo-driver 561-674-7866"
ROSTER = [Member(8063167928, "M Mgn", None, False),
          Member(6066541941, "Noor Dubat", "noor", False)]


def _wire(monkeypatch, *, lookup, description=ABOUT, members=ROSTER):
    """Stub everything start_onboarding touches except the decision itself."""
    monkeypatch.setattr(onboard.userbot, "list_members",
                        AsyncMock(return_value=list(members)))
    monkeypatch.setattr(onboard.userbot, "get_description",
                        AsyncMock(return_value=description))
    monkeypatch.setattr(onboard, "get_active_units", AsyncMock(return_value=set()))
    monkeypatch.setattr(onboard, "get_non_driver_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(onboard, "_ADMIN_IDS", [7564871221])
    monkeypatch.setattr(onboard.phone_lookup, "is_configured", lambda: True)
    monkeypatch.setattr(onboard.phone_lookup, "lookup", lookup)

    writes = {
        "set_group_unit": AsyncMock(),
        "replace_drivers": AsyncMock(),
        "unmark_non_drivers": AsyncMock(),
    }
    for name, mock in writes.items():
        monkeypatch.setattr(onboard, name, mock)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(onboard.bot, "send_message", sent)
    return writes, sent


def test_a_clean_group_configures_itself_and_only_reports(monkeypatch):
    resolved = {"+17864882619": Match("+17864882619", 8063167928, "M Mgn", None, False),
                "+15616747866": Match("+15616747866", 6066541941, "Noor Dubat",
                                      "noor", False)}
    writes, sent = _wire(monkeypatch, lookup=AsyncMock(return_value=resolved))

    assert asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH")) is True

    writes["set_group_unit"].assert_awaited_once_with(-100123, "1216")
    assert writes["replace_drivers"].await_args.args[1] == [
        {"user_id": 8063167928, "name": "M Mgn"},
        {"user_id": 6066541941, "name": "Noor Dubat"},
    ]
    text, kwargs = sent.await_args.args[1], sent.await_args.kwargs
    assert "configured automatically" in text
    # The notice is informational, but a wrong save has to be one tap from the
    # normal picker -- otherwise the admin's only recourse is /onboard.
    assert "ob:e:-100123" in str(kwargs["reply_markup"])


def test_a_driver_past_the_button_limit_is_still_a_member(monkeypatch):
    """The keyboard holds MEMBER_BUTTONS members; the chat holds far more.

    Slicing the roster for the buttons and then reusing that slice as the
    membership evidence made real drivers read as "not in this group": on the
    JRD fleet on 2026-08-14 this left 8 of 11 groups unconfigurable, and the
    picker offered no button for them either, so there was no way through by
    hand. Every one of those groups had 38-59 members.
    """
    padding = [Member(900_000 + i, f"Dispatch {i}", None, False)
               for i in range(onboard.MEMBER_BUTTONS)]
    late = Member(6066541941, "Noor Dubat", "noor", False)
    roster = [Member(8063167928, "M Mgn", None, False)] + padding + [late]
    assert roster.index(late) > onboard.MEMBER_BUTTONS

    resolved = {"+17864882619": Match("+17864882619", 8063167928, "M Mgn", None, False),
                "+15616747866": Match("+15616747866", 6066541941, "Noor Dubat",
                                      "noor", False)}
    writes, _ = _wire(monkeypatch, lookup=AsyncMock(return_value=resolved),
                      members=roster)

    assert asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH")) is True

    writes["set_group_unit"].assert_awaited_once_with(-100123, "1216")
    assert writes["replace_drivers"].await_args.args[1] == [
        {"user_id": 8063167928, "name": "M Mgn"},
        {"user_id": 6066541941, "name": "Noor Dubat"},
    ]


def test_drivers_are_stored_under_their_fleet_names(monkeypatch):
    """A Telegram profile says "Emile ✈️"; the driver list says ZAMA, EMILE."""
    resolved = {"+17188641154": Match("+17188641154", 8063167928, "Emile ✈️",
                                      None, False),
                "+15616674276": Match("+15616674276", 6066541941, "jacques_f",
                                      "jacques_f", False)}
    members = [Member(8063167928, "Emile ✈️", None, False),
               Member(6066541941, "jacques_f", "jacques_f", False)]
    writes, sent = _wire(monkeypatch, lookup=AsyncMock(return_value=resolved),
                         description=ABOUT_NAMED, members=members)

    asyncio.run(onboard.start_onboarding(-100123, "1239 ZAMA / FLEURMOND"))

    assert writes["replace_drivers"].await_args.args[1] == [
        {"user_id": 8063167928, "name": "Zama Emile"},
        {"user_id": 6066541941, "name": "Fleurmond Jacques"},
    ]
    # Both names in the notice: the fleet's is what was stored, Telegram's is
    # how the admin recognises the person it was stored against.
    text = sent.await_args.args[1]
    assert "Zama Emile" in text and "Emile ✈️" in text


def test_an_unavailable_lookup_falls_back_to_the_picker(monkeypatch):
    """A rate-limited account may cost an automatic setup, never a wrong one."""
    writes, sent = _wire(
        monkeypatch,
        lookup=AsyncMock(side_effect=LookupUnavailable("contact-import limited")))

    assert asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH")) is True

    writes["set_group_unit"].assert_not_awaited()
    writes["replace_drivers"].assert_not_awaited()
    text = sent.await_args.args[1]
    assert "who are the drivers" in text
    assert "Couldn't do this automatically" in text


def test_the_picker_says_why_it_could_not_be_automatic(monkeypatch):
    resolved = {"+17864882619": Match("+17864882619", 8063167928, "M Mgn", None, False),
                "+15616747866": None}
    writes, sent = _wire(monkeypatch, lookup=AsyncMock(return_value=resolved))

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    writes["replace_drivers"].assert_not_awaited()
    assert "+15616747866 matched no Telegram account" in sent.await_args.args[1]


def test_without_a_lookup_session_nothing_changes(monkeypatch):
    """The feature is gated on the second session existing, and its absence is
    the ordinary state for a dev box -- it must not add a scary note."""
    writes, sent = _wire(monkeypatch, lookup=AsyncMock())
    monkeypatch.setattr(onboard.phone_lookup, "is_configured", lambda: False)

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    writes["replace_drivers"].assert_not_awaited()
    assert "Couldn't do this automatically" not in sent.await_args.args[1]
