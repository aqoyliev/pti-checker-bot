"""The derived "is this group still in use?" status.

Pure: no DB, no aiogram. Mirrors the offline rule scripts/tg_scan.py uses —
a group is dormant when no *human* has posted in it for GROUP_INACTIVE_DAYS.
"""
from datetime import datetime, timedelta

from utils.group_activity import (
    describe_quiet,
    is_dormant,
    last_human_at,
    quiet_days,
)

NOW = datetime(2026, 8, 10, 12, 0, 0)


def group(**kw) -> dict:
    g = {"group_id": -100123, "created_at": NOW - timedelta(days=365)}
    g.update(kw)
    return g


def test_recent_human_message_is_not_dormant():
    g = group(last_human_message_at=NOW - timedelta(hours=6))
    assert not is_dormant(g, now=NOW)
    assert quiet_days(g, now=NOW) == 0


def test_quiet_for_three_days_is_dormant():
    g = group(last_human_message_at=NOW - timedelta(days=3, minutes=1))
    assert is_dormant(g, now=NOW)
    assert quiet_days(g, now=NOW) == 3


def test_boundary_is_inclusive_at_exactly_three_days():
    assert is_dormant(group(last_human_message_at=NOW - timedelta(days=3)), now=NOW)
    assert not is_dormant(
        group(last_human_message_at=NOW - timedelta(days=3) + timedelta(minutes=1)), now=NOW
    )


def test_window_is_configurable():
    g = group(last_human_message_at=NOW - timedelta(days=5))
    assert is_dormant(g, now=NOW, days=3)
    assert not is_dormant(g, now=NOW, days=7)


def test_never_spoken_falls_back_to_creation_time():
    """A group added months ago that has never said a word is dormant; one added
    this morning is simply too young to judge."""
    assert is_dormant(group(created_at=NOW - timedelta(days=30)), now=NOW)
    assert not is_dormant(group(created_at=NOW - timedelta(hours=2)), now=NOW)


def test_no_timestamps_at_all_is_not_dormant():
    """Unknown is not the same as dead — don't label a group on no evidence."""
    g = {"group_id": -100999}
    assert not is_dormant(g, now=NOW)
    assert quiet_days(g, now=NOW) is None
    assert describe_quiet(g, now=NOW) == "—"


def test_tz_aware_timestamps_are_accepted():
    from datetime import timezone

    aware = (NOW - timedelta(days=4)).replace(tzinfo=timezone.utc)
    g = group(last_human_message_at=aware)
    assert last_human_at(g).tzinfo is None
    assert is_dormant(g, now=NOW)


def test_clock_skew_does_not_produce_negative_ages():
    g = group(last_human_message_at=NOW + timedelta(minutes=5))
    assert quiet_days(g, now=NOW) == 0
    assert not is_dormant(g, now=NOW)


def test_describe_quiet_units():
    assert describe_quiet(group(last_human_message_at=NOW - timedelta(minutes=20)), now=NOW) == "20m"
    assert describe_quiet(group(last_human_message_at=NOW - timedelta(hours=5)), now=NOW) == "5h"
    assert describe_quiet(group(last_human_message_at=NOW - timedelta(days=9)), now=NOW) == "9d"
