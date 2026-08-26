"""A group upgraded to a supergroup must be followed, not written off.

Telegram replaces the chat id outright on the upgrade and answers every send to
the old one with MigrateToChat. Until the rows follow, the unit is broken both
ways: reminders bounce off a dead id, and a PTI posted in the new chat finds no
``groups`` row and is refused silently. Three groups sat like that on
2026-08-25, one hourly error each and no inspections at all.
"""
from __future__ import annotations

import asyncio

from aiogram.utils.exceptions import ChatNotFound, MigrateToChat

import utils.reminders as reminders

OLD, NEW = -5403529040, -1004406318181


class _Recorder:
    def __init__(self, migrates: bool = True):
        self.migrated: list[tuple[int, int]] = []
        self.strikes: dict[int, int] = {}
        self.deactivated: list[int] = []
        self._migrates = migrates

    async def migrate_group_id(self, old_id: int, new_id: int) -> bool:
        self.migrated.append((old_id, new_id))
        return self._migrates

    async def mark_unreachable(self, group_id: int) -> int:
        self.strikes[group_id] = self.strikes.get(group_id, 0) + 1
        return self.strikes[group_id]

    async def clear_unreachable(self, group_id: int) -> None:
        self.strikes[group_id] = 0

    async def mark_group_inactive(self, group_id: int) -> None:
        self.deactivated.append(group_id)


def _patch(monkeypatch, rec, exc):
    sent: list[int] = []

    async def send_message(chat_id, *a, **k):
        sent.append(chat_id)
        if exc is not None:
            raise exc
    monkeypatch.setattr(reminders.bot, "send_message", send_message)
    for name in ("migrate_group_id", "mark_unreachable",
                 "clear_unreachable", "mark_group_inactive"):
        monkeypatch.setattr(reminders, name, getattr(rec, name))
    return sent


def test_migration_moves_the_group(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec, MigrateToChat(NEW))
    asyncio.run(reminders._send(OLD, "hi"))
    assert rec.migrated == [(OLD, NEW)]


def test_migration_is_not_an_unreachable_strike(monkeypatch):
    """The chat moved; it is not gone. A strike here would eventually
    deactivate a perfectly live group."""
    rec = _Recorder()
    _patch(monkeypatch, rec, MigrateToChat(NEW))
    asyncio.run(reminders._send(OLD, "hi"))
    assert rec.strikes == {}
    assert rec.deactivated == []


def test_migration_defers_the_send(monkeypatch):
    """False, so the caller writes nothing for this pass.

    Every stamp the caller makes -- last_reminder_at, overdue_reminded_at -- is
    keyed on the group_id that has just moved, so re-sending here would deliver
    a reminder whose 24-hour slot never got stamped and the unit would hear it
    again an hour later.
    """
    rec = _Recorder()
    sent = _patch(monkeypatch, rec, MigrateToChat(NEW))
    assert asyncio.run(reminders._send(OLD, "hi")) is False
    assert sent == [OLD]  # not re-sent to NEW


def test_unmovable_group_still_defers(monkeypatch):
    """The new id already holds a group: merging two is a decision, not a
    repair, so nothing is written and nothing is sent."""
    rec = _Recorder(migrates=False)
    _patch(monkeypatch, rec, MigrateToChat(NEW))
    assert asyncio.run(reminders._send(OLD, "hi")) is False
    assert rec.deactivated == []


def test_ordinary_unreachable_is_untouched(monkeypatch):
    rec = _Recorder()
    _patch(monkeypatch, rec, ChatNotFound("chat not found"))
    assert asyncio.run(reminders._send(OLD, "hi")) is False
    assert rec.migrated == []
    assert rec.strikes[OLD] == 1
