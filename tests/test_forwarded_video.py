"""Unit tests for the auto-inspect forwarded-video guard.

A driver's auto-inspected video must be their own fresh recording. A forwarded
clip (e.g. a driver forwarding someone else's video) must be ignored silently,
so `_is_forwarded_from_other` decides whether the auto-inspector skips it.
"""
from types import SimpleNamespace

from handlers.groups.pti import _is_forwarded_from_other

DRIVER_UID = 1000


def _msg(**kwargs):
    """A minimal stand-in for aiogram's Message with only the forward fields."""
    base = {
        "forward_date": None,
        "forward_from": None,
        "forward_from_chat": None,
        "forward_sender_name": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_not_forwarded_is_allowed():
    assert _is_forwarded_from_other(_msg(), DRIVER_UID) is False


def test_forwarded_from_another_user_is_blocked():
    msg = _msg(forward_date=123, forward_from=SimpleNamespace(id=DRIVER_UID + 1))
    assert _is_forwarded_from_other(msg, DRIVER_UID) is True


def test_forwarded_from_self_is_allowed():
    msg = _msg(forward_date=123, forward_from=SimpleNamespace(id=DRIVER_UID))
    assert _is_forwarded_from_other(msg, DRIVER_UID) is False


def test_forwarded_from_channel_is_blocked():
    msg = _msg(forward_date=123, forward_from_chat=SimpleNamespace(id=-100123))
    assert _is_forwarded_from_other(msg, DRIVER_UID) is True


def test_forwarded_with_hidden_sender_is_blocked():
    msg = _msg(forward_date=123, forward_sender_name="Hidden User")
    assert _is_forwarded_from_other(msg, DRIVER_UID) is True


def test_forward_date_only_is_blocked():
    # Origin can't be attributed to the driver -> treat as "from someone else".
    assert _is_forwarded_from_other(_msg(forward_date=123), DRIVER_UID) is True
