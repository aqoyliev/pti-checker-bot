"""The compliance loop reminds and reports; it never restricts anyone.

The rule is structural, not a config flag: the module must have no way to
call ``restrict_chat_member`` at all, in either direction. An unmute helper
used to exist to lift restrictions from before the rule; it ran only behind
``ENFORCEMENT_ENABLED``, which no fleet has ever turned on, and was removed
on 2026-09-13.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from utils import enforcement


def test_there_is_no_way_to_restrict_a_driver():
    assert not hasattr(enforcement, "mute_driver")
    assert not hasattr(enforcement, "unmute_driver")
    assert not hasattr(enforcement, "_MUTED_PERMISSIONS")
    # The module may *talk* about the call in its docstring; it must not make it.
    source = open(enforcement.__file__, encoding="utf-8").read()
    assert ".restrict_chat_member(" not in source


def _fake_loop_env(monkeypatch, restrict_perms, sent, group=None, drivers=None):
    async def fake_restrict(group_id, user_id, permissions=None):
        restrict_perms.append(permissions)

    async def fake_send(group_id, text, **kwargs):
        sent.append(text)

    async def fake_get_chat(group_id):
        return type("Chat", (), {"title": "G"})()

    monkeypatch.setattr(enforcement, "get_all_registered_groups",
                        AsyncMock(return_value=[group or {"group_id": -100}]))
    monkeypatch.setattr(enforcement, "get_drivers",
                        AsyncMock(return_value=drivers or [{"user_id": 1, "name": "Bob"}]))
    monkeypatch.setattr(enforcement, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(enforcement.bot, "get_chat", fake_get_chat)
    monkeypatch.setattr(enforcement.bot, "restrict_chat_member", fake_restrict)
    monkeypatch.setattr(enforcement.bot, "send_message", fake_send)


def test_disabled_loop_does_nothing(monkeypatch):
    # Reminders off: no restriction calls and no messages. The loop bails before
    # touching a group.
    assert enforcement.ENFORCEMENT_ENABLED is False

    restrict_perms, sent = [], []
    _fake_loop_env(monkeypatch, restrict_perms, sent)

    asyncio.run(enforcement.run_compliance_check())

    assert restrict_perms == []
    assert sent == []


def test_overdue_driver_is_reminded_never_restricted(monkeypatch):
    # With reminders on, an overdue driver gets a message and shows up in the
    # admin report — but the bot still makes no restriction call at all.
    restrict_perms, sent = [], []
    _fake_loop_env(monkeypatch, restrict_perms, sent)
    monkeypatch.setattr(enforcement, "ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(enforcement, "check_driver_compliance",
                        AsyncMock(return_value=(False, "no PTI submitted yet this week")))

    asyncio.run(enforcement.run_compliance_check())

    assert restrict_perms == []
    assert any("overdue" in t for t in sent)
    assert not any("restricted" in t.lower() for t in sent)


def test_a_unit_reminded_today_is_not_reminded_again(monkeypatch):
    # The loop runs hourly. Without the 24-hour gate an overdue driver is told
    # once an hour, all day.
    restrict_perms, sent = [], []
    _fake_loop_env(monkeypatch, restrict_perms, sent,
                   group={"group_id": -100,
                          "last_reminder_at": datetime.utcnow() - timedelta(hours=2)})
    monkeypatch.setattr(enforcement, "ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(enforcement, "check_driver_compliance",
                        AsyncMock(return_value=(False, "no PTI submitted yet this week")))
    monkeypatch.setattr(enforcement, "notify_admins", AsyncMock())

    asyncio.run(enforcement.run_compliance_check())

    assert sent == []


def test_both_overdue_drivers_share_one_message(monkeypatch):
    # The cap is per unit, so two overdue drivers on one truck are named in a
    # single reminder rather than getting one each.
    restrict_perms, sent = [], []
    _fake_loop_env(monkeypatch, restrict_perms, sent,
                   drivers=[{"user_id": 1, "name": "Bob"}, {"user_id": 2, "name": "Ann"}])
    monkeypatch.setattr(enforcement, "ENFORCEMENT_ENABLED", True)
    monkeypatch.setattr(enforcement, "check_driver_compliance",
                        AsyncMock(return_value=(False, "no PTI submitted yet this week")))
    monkeypatch.setattr(enforcement, "notify_admins", AsyncMock())

    asyncio.run(enforcement.run_compliance_check())

    assert len(sent) == 1
    assert "Bob" in sent[0] and "Ann" in sent[0]
