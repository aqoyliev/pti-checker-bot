"""Reminders tag drivers as real Telegram mentions, not plain text.

Pure: no network. A reminder that names a driver without pinging them is the
failure this guards against.
"""
from utils.reminders import _driver_names, _driver_names_plain


def test_driver_is_a_tappable_mention():
    out = _driver_names([{"user_id": 111, "name": "Ana"}])
    assert out == '<a href="tg://user?id=111">Ana</a>'


def test_several_drivers_are_each_mentioned():
    out = _driver_names([{"user_id": 1, "name": "Ana"}, {"user_id": 2, "name": "Bo"}])
    assert out == '<a href="tg://user?id=1">Ana</a>, <a href="tg://user?id=2">Bo</a>'


def test_name_is_html_escaped_inside_the_mention():
    # Driver names come from Telegram profiles and go into a parse_mode="HTML"
    # message, so an unescaped "<" would break the whole reminder.
    out = _driver_names([{"user_id": 5, "name": "A <b>& Co"}])
    assert out == '<a href="tg://user?id=5">A &lt;b&gt;&amp; Co</a>'


def test_driver_without_user_id_still_appears_as_text():
    # Dropping them would silently omit a driver from their own reminder.
    out = _driver_names([{"name": "Ana"}, {"user_id": 2, "name": "Bo"}])
    assert out == 'Ana, <a href="tg://user?id=2">Bo</a>'


def test_nameless_rows_are_skipped():
    assert _driver_names([{"user_id": 9, "name": ""}, {"user_id": 8, "name": "Bo"}]) == \
        '<a href="tg://user?id=8">Bo</a>'


def test_no_drivers_falls_back_to_a_generic_word():
    assert _driver_names([]) == "Driver"


def test_email_summary_stays_plain_text():
    # The overdue email is not HTML — mention markup there would be noise.
    assert _driver_names_plain([{"user_id": 1, "name": "Ana"}]) == "Ana"
