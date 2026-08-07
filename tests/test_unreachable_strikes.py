"""A single unreachable send must not deactivate a group.

Regression cover for the incident where a redeploy wiped the local Bot API
server's chat state, it answered "chat not found" for groups the bot was still
in, and the reminder pass deactivated 19 healthy groups in one sweep.
"""
from __future__ import annotations

import asyncio

import pytest
from aiogram.utils.exceptions import ChatNotFound

import utils.reminders as reminders


class _Recorder:
    def __init__(self):
        self.strikes: dict[int, int] = {}
        self.deactivated: list[int] = []
        self.cleared: list[int] = []

    async def mark_unreachable(self, group_id: int) -> int:
        self.strikes[group_id] = self.strikes.get(group_id, 0) + 1
        return self.strikes[group_id]

    async def clear_unreachable(self, group_id: int) -> None:
        self.cleared.append(group_id)
        self.strikes[group_id] = 0

    async def mark_group_inactive(self, group_id: int) -> None:
        self.deactivated.append(group_id)


@pytest.fixture
def rec(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(reminders, "mark_unreachable", r.mark_unreachable)
    monkeypatch.setattr(reminders, "clear_unreachable", r.clear_unreachable)
    monkeypatch.setattr(reminders, "mark_group_inactive", r.mark_group_inactive)
    return r


def _bot_that_fails(monkeypatch):
    async def send_message(*a, **k):
        raise ChatNotFound("chat not found")
    monkeypatch.setattr(reminders.bot, "send_message", send_message)


def _bot_that_works(monkeypatch):
    async def send_message(*a, **k):
        return None
    monkeypatch.setattr(reminders.bot, "send_message", send_message)


def test_single_failure_does_not_deactivate(rec, monkeypatch):
    _bot_that_fails(monkeypatch)
    assert asyncio.run(reminders._send(-100, "hi")) is False
    assert rec.deactivated == []
    assert rec.strikes[-100] == 1


def test_deactivates_only_after_limit(rec, monkeypatch):
    _bot_that_fails(monkeypatch)
    for _ in range(reminders.UNREACHABLE_LIMIT - 1):
        asyncio.run(reminders._send(-100, "hi"))
    assert rec.deactivated == []

    asyncio.run(reminders._send(-100, "hi"))
    assert rec.deactivated == [-100]


def test_success_resets_the_streak(rec, monkeypatch):
    _bot_that_fails(monkeypatch)
    asyncio.run(reminders._send(-100, "hi"))
    assert rec.strikes[-100] == 1

    _bot_that_works(monkeypatch)
    assert asyncio.run(reminders._send(-100, "hi")) is True
    assert rec.strikes[-100] == 0

    # A later failure starts from scratch, so intermittent errors never add up.
    _bot_that_fails(monkeypatch)
    asyncio.run(reminders._send(-100, "hi"))
    assert rec.strikes[-100] == 1
    assert rec.deactivated == []
