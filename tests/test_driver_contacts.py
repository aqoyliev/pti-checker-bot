"""Pairing a driver's name with their phone number out of a group's About text.

The layouts here are copied from live fleet groups. All four were measured
against 60 of them, and all 60 paired; the cases that must *not* pair are the
point of the rest.

Pure: no network, no database.
"""
from utils.driver_names import parse_driver_contacts, parse_driver_names


# ---------- layouts the fleet actually writes ----------

def test_name_and_number_on_the_same_line():
    about = ("Truck # 2588\n"
             "Trailer # \n"
             "Driver 1 - STIMPHILE, KESLIN - 786-488-2619\n"
             "Driver 2 - DUCASSE, WILLIAMSON - 561-667-4276\n\n"
             "Despir Trained")
    assert parse_driver_contacts(about) == [
        ("Stimphile Keslin", "786-488-2619"),
        ("Ducasse Williamson", "561-667-4276"),
    ]


def test_bare_name_line_directly_above_the_numbers():
    # No label at all -- parse_driver_names refuses this on its own, and should:
    # a bare line of prose is not a name list. Sitting immediately above a
    # matching count of phone numbers is the evidence it otherwise lacks.
    about = ("Truck - 2589\n"
             "MATTHEWS, CHRISTOPHER / MCLAUGHLIN, ARTESIA\n"
             "718-864-1154 / 561-667-4276\n"
             "Trailer -\n\n"
             "Home in Baltimore AUGUST18-20. Need to be there on the 21.")
    assert parse_driver_names(about) == []
    assert parse_driver_contacts(about) == [
        ("Matthews Christopher", "718-864-1154"),
        ("Mclaughlin Artesia", "561-667-4276"),
    ]


def test_labelled_lines_without_a_separator():
    about = ("Truck 1318\n"
             "Trailer 530812\n"
             "Drivers AWAT, AHMED HASSAN / ABDULLAHI,ABDULWAHAB\n"
             "Numbers 718-864-1154 / 561-667-4276")
    assert parse_driver_contacts(about) == [
        ("Awat Ahmed Hassan", "718-864-1154"),
        ("Abdullahi Abdulwahab", "561-667-4276"),
    ]


def test_labelled_parallel_lines():
    about = ("Name: ZAMA, EMILE / FLEURMOND, JACQUES\n"
             "Phone# 718-864-1154 / 561-667-4276\n"
             "Truck# 1239")
    assert parse_driver_contacts(about) == [
        ("Zama Emile", "718-864-1154"),
        ("Fleurmond Jacques", "561-667-4276"),
    ]


def test_the_number_comes_back_as_the_fleet_typed_it():
    # Not normalized: this is shown to a person who has to recognise and dial it.
    (_, phone), = parse_driver_contacts("Driver: ZAMA, EMILE - (561) 674-7866")
    assert phone == "(561) 674-7866"


# ---------- what must not be paired ----------

def test_a_mismatched_count_pairs_nothing():
    # Three numbers, two names: one of them belongs to dispatch, and guessing
    # which is exactly the failure this exists to avoid.
    about = ("Name: ZAMA, EMILE / FLEURMOND, JACQUES\n"
             "718-864-1154 / 561-667-4276 / 305-111-2222")
    assert parse_driver_contacts(about) == [
        ("Zama Emile", None), ("Fleurmond Jacques", None),
    ]


def test_numbers_with_no_names_attach_to_nobody():
    assert parse_driver_contacts("Truck# 1216\n718-864-1154\n561-667-4276") == []


def test_a_label_line_is_not_a_name():
    assert parse_driver_contacts("Phone# 718-864-1154") == []


def test_a_unit_number_is_not_a_phone_number():
    about = "Truck# 147085\nTrailer# TM530812\nName: ZAMA, EMILE"
    assert parse_driver_contacts(about) == [("Zama Emile", None)]


def test_empty_text():
    assert parse_driver_contacts("") == []
    assert parse_driver_contacts(None) == []
