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
    # The caller passes None whenever the title and description yield no
    # number at all.
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
    monkeypatch.setattr(onboard, "get_non_driver_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(onboard, "_ADMIN_IDS", [7564871221])
    monkeypatch.setattr(onboard.phone_lookup, "is_configured", lambda: True)
    monkeypatch.setattr(onboard.phone_lookup, "lookup", lookup)

    writes = {
        "set_group_unit": AsyncMock(),
        "replace_drivers": AsyncMock(),
        "unmark_non_drivers": AsyncMock(),
        "mark_non_drivers": AsyncMock(return_value=0),
        "get_registered_driver_ids": AsyncMock(return_value=set()),
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
    # Informational, and nothing more: no button on a setup that went right.
    # The way to change one is named in the text instead.
    assert "reply_markup" not in kwargs
    assert "/onboard -100123" in text


def test_manual_onboard_of_a_clean_group_still_offers_an_edit_button(monkeypatch):
    """/onboard <group_id> is a deliberate admin request, unlike the passive
    join/nag trigger -- a clean auto-config still gets an Edit button, since an
    admin who explicitly asked to review this group has nothing to fall back
    on besides manual /adddriver commands otherwise.
    """
    resolved = {"+17864882619": Match("+17864882619", 8063167928, "M Mgn", None, False),
                "+15616747866": Match("+15616747866", 6066541941, "Noor Dubat",
                                      "noor", False)}
    _, sent = _wire(monkeypatch, lookup=AsyncMock(return_value=resolved))

    assert asyncio.run(
        onboard.start_onboarding(-100123, "UNIT 1216 SMITH", manual=True)) is True

    kwargs = sent.await_args.kwargs
    assert kwargs["reply_markup"] is not None


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


# ---------- the rest of the roster ----------

def _clean_lookup():
    return AsyncMock(return_value={
        "+17864882619": Match("+17864882619", 8063167928, "M Mgn", None, False),
        "+15616747866": Match("+15616747866", 6066541941, "Noor Dubat",
                              "noor", False)})


def test_everyone_else_in_the_group_is_recorded_as_a_non_driver(monkeypatch):
    """The point of the whole table: stop offering the same office staff.

    This path knows who the drivers are from the fleet's own phone numbers, so
    the rest of the chat is dispatch, safety or a mechanic -- better evidence
    than a picker tap, not worse.
    """
    roster = ROSTER + [Member(555, "Dispatch", None, False),
                       Member(556, "Safety", "safety", False)]
    writes, _ = _wire(monkeypatch, lookup=_clean_lookup(), members=roster)

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    assert writes["mark_non_drivers"].await_args.args[0] == [
        (555, "Dispatch"), (556, "Safety")]
    # And being a driver still outranks a stale row, as everywhere else.
    assert writes["unmark_non_drivers"].await_args.args[0] == [8063167928, 6066541941]


def test_a_driver_of_another_group_is_not_swept_up(monkeypatch):
    """A team driver who changed trucks sits in two chats. Hiding them
    fleet-wide would cost the next group's prompt its buttons."""
    roster = ROSTER + [Member(555, "Dispatch", None, False),
                       Member(777, "Drives 1102", None, False)]
    writes, _ = _wire(monkeypatch, lookup=_clean_lookup(), members=roster)
    writes["get_registered_driver_ids"].return_value = {777}

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    assert writes["mark_non_drivers"].await_args.args[0] == [(555, "Dispatch")]


def test_the_whole_roster_is_judged_not_just_the_keyboard(monkeypatch):
    """MEMBER_BUTTONS caps what an admin can *see*, and nobody sees this."""
    padding = [Member(900_000 + i, f"Dispatch {i}", None, False)
               for i in range(onboard.MEMBER_BUTTONS + 5)]
    writes, _ = _wire(monkeypatch, lookup=_clean_lookup(), members=ROSTER + padding)

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    assert len(writes["mark_non_drivers"].await_args.args[0]) == len(padding)


def test_a_group_of_nothing_but_drivers_marks_nobody(monkeypatch):
    writes, _ = _wire(monkeypatch, lookup=_clean_lookup())

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    assert writes["mark_non_drivers"].await_args.args[0] == []


def test_the_notice_says_a_fleet_wide_exclusion_was_written(monkeypatch):
    """It is the one moment an admin can see this happen, and it is reversible
    two ways -- so the notice has to name both."""
    roster = ROSTER + [Member(555, "Dispatch", None, False)]
    writes, sent = _wire(monkeypatch, lookup=_clean_lookup(), members=roster)
    writes["mark_non_drivers"].return_value = 1

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    text = sent.await_args.args[1]
    assert "1 other member(s)" in text
    assert "/nondrivers clear" in text


def test_nothing_new_to_hide_says_nothing(monkeypatch):
    """Everyone was already on the list: a re-run must not report a change."""
    roster = ROSTER + [Member(555, "Dispatch", None, False)]
    _, sent = _wire(monkeypatch, lookup=_clean_lookup(), members=roster)

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    assert "recorded as non-drivers" not in sent.await_args.args[1]


def test_the_picker_path_marks_nobody_up_front(monkeypatch):
    """Declining is not a judgement on the roster -- only a Save is."""
    resolved = {"+17864882619": Match("+17864882619", 8063167928, "M Mgn",
                                      None, False),
                "+15616747866": None}
    writes, _ = _wire(monkeypatch, lookup=AsyncMock(return_value=resolved),
                      members=ROSTER + [Member(555, "Dispatch", None, False)])

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    writes["mark_non_drivers"].assert_not_awaited()


def test_a_setup_short_of_a_driver_sweeps_nobody(monkeypatch):
    """One number for two names configures a solo driver -- and the co-driver
    it could not place is still in that roster. Hiding them would bury the one
    person the group is missing."""
    one_phone = "UNIT 1216\nName: ABDULAHI MAHAMED / JAMA MOHAMED\nPhone# 786-488-2619"
    roster = ROSTER + [Member(555, "Dispatch", None, False)]
    writes, _ = _wire(
        monkeypatch, description=one_phone, members=roster,
        lookup=AsyncMock(return_value={
            "+17864882619": Match("+17864882619", 8063167928, "M Mgn", None, False)}))

    asyncio.run(onboard.start_onboarding(-100123, "UNIT 1216 SMITH"))

    assert writes["replace_drivers"].await_args.args[1] == [
        {"user_id": 8063167928, "name": "M Mgn"}]
    writes["mark_non_drivers"].assert_not_awaited()
