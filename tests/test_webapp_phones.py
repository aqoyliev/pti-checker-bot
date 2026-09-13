"""Putting a driver's phone number on their row in the panel.

The number an admin needs is already written in the group's About text, which
the panel fetches anyway for the title. Getting it onto the right row needs two
pairings to hold at once — About text name to number, and name to registered
`user_id` — and the second one refuses to guess. Whatever is left unattributed
is still shown, as the *group's* numbers: an admin looking up who to call would
rather see two numbers than none, and no driver row claims one that may not be
theirs.

Pure: no network, no database.
"""
from webapp.server import _driver_phones

ABOUT = ("Truck # 2588\n"
         "Driver 1 - STIMPHILE, KESLIN - 786-488-2619\n"
         "Driver 2 - DUCASSE, WILLIAMSON - 561-667-4276")

DRIVERS = [{"user_id": 11, "name": "Stimphile Keslin"},
           {"user_id": 22, "name": "Ducasse Williamson"}]


def test_each_driver_gets_their_own_number():
    phones, spare = _driver_phones(ABOUT, DRIVERS)
    assert phones == {11: "786-488-2619", 22: "561-667-4276"}
    assert spare == []


def test_a_telegram_handle_still_pairs_by_its_words():
    # The stored name is often the fleet's ("Stimphile Keslin"), but a group
    # configured before that has whatever Telegram said.
    phones, _ = _driver_phones(ABOUT, [{"user_id": 11, "name": "Keslin 🇭🇹"}])
    assert phones == {11: "786-488-2619"}


def test_an_unmatched_number_is_shown_as_the_groups_own():
    phones, spare = _driver_phones(ABOUT, [{"user_id": 11, "name": "Stimphile Keslin"}])
    assert phones == {11: "786-488-2619"}
    assert spare == ["561-667-4276"]


def test_a_group_with_nobody_registered_still_shows_both_numbers():
    phones, spare = _driver_phones(ABOUT, [])
    assert phones == {}
    assert spare == ["786-488-2619", "561-667-4276"]


def test_two_drivers_sharing_a_surname_pair_to_neither():
    # match_names_to_drivers refuses an unproven pairing, and a wrong phone
    # number on a driver row would have someone call the wrong person.
    about = ("Driver 1 - MOHAMED, ABDIKAFAR - 786-488-2619\n"
             "Driver 2 - MOHAMED, DAUD JAILANI - 561-667-4276")
    phones, spare = _driver_phones(about, [{"user_id": 11, "name": "Mohamed Abdikafar"},
                                           {"user_id": 22, "name": "Mohamed Daud"}])
    assert phones == {}
    assert spare == ["786-488-2619", "561-667-4276"]


def test_an_about_text_with_no_numbers_yields_nothing():
    phones, spare = _driver_phones("Truck# 2588\nHome in Baltimore", DRIVERS)
    assert phones == {} and spare == []


def test_an_unreadable_about_text_is_not_an_error():
    assert _driver_phones("", DRIVERS) == ({}, [])
