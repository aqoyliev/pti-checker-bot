"""The quiet-group threshold. Pure: no DB.

The rule is "at most N human messages in the window", not "zero", so the
boundary is the whole point of these tests.
"""
from utils.group_activity import is_quiet, quiet_groups


def _g(gid: int, unit=None, active=True) -> dict:
    return {"group_id": gid, "unit_number": unit or str(gid), "is_active": active,
            "title": f"Truck {unit or gid}"}


# ---------- is_quiet ----------

def test_no_messages_is_quiet():
    assert is_quiet(0) is True


def test_never_seen_group_is_quiet_not_unknown():
    # No bucket row at all — a chat that has never spoken.
    assert is_quiet(None) is True


def test_a_few_messages_still_counts_as_quiet():
    # A stray "ok" or a sticker is not evidence a truck is in service.
    assert is_quiet(1) is True
    assert is_quiet(3) is True


def test_above_the_threshold_is_not_quiet():
    assert is_quiet(4) is False
    assert is_quiet(40) is False


def test_threshold_is_configurable():
    assert is_quiet(3, max_messages=0) is False
    assert is_quiet(0, max_messages=0) is True


# ---------- quiet_groups ----------

def test_selects_only_the_quiet_ones():
    groups = [_g(1), _g(2), _g(3)]
    counts = {1: 0, 2: 9, 3: 2}
    assert [g["group_id"] for g in quiet_groups(groups, counts)] == [1, 3]


def test_group_missing_from_counts_is_quiet():
    assert [g["group_id"] for g in quiet_groups([_g(1)], {})] == [1]


def test_deactivated_groups_are_excluded():
    # Already known-retired; listing them would bury the live trucks that just
    # went silent, which are the only ones worth looking at.
    groups = [_g(1, active=False), _g(2)]
    assert [g["group_id"] for g in quiet_groups(groups, {1: 0, 2: 0})] == [2]


def test_sorted_quietest_first():
    groups = [_g(1), _g(2), _g(3)]
    counts = {1: 3, 2: 0, 3: 1}
    assert [g["group_id"] for g in quiet_groups(groups, counts)] == [2, 3, 1]


def test_missing_is_active_key_defaults_to_active():
    out = quiet_groups([{"group_id": 7, "unit_number": "1332"}], {})
    assert [g["group_id"] for g in out] == [7]


def test_empty_fleet_is_not_an_error():
    assert quiet_groups([], {}) == []
