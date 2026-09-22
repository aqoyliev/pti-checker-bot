"""The title sweep revives a retired group only if the bot is still in its chat.

A basic group answers getChat for a bot that was kicked from it -- title and
all -- so a readable title that names a unit is not proof the chat can be
reached. On JRD, 2026-09-20..22, two kicked groups were switched back on every
morning, retired again by three BotKicked reminders, and reported to the
admins as "2 group(s) reactivated" each day. Telegram is stubbed here; nothing
touches the network or the database.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.admin import units
from handlers.admin.units import title_reactivations


def _off(gid: int, unit, title, by="unreachable") -> dict:
    return {"group_id": gid, "unit_number": unit, "title": title,
            "is_active": False, "deactivated_by": by}


def _members(monkeypatch, statuses: dict):
    """getChatMember stub: group id -> a status, a member object, or an error."""
    async def get_chat_member(gid, user_id):
        answer = statuses[gid]
        if isinstance(answer, Exception):
            raise answer
        return answer if not isinstance(answer, str) else SimpleNamespace(status=answer)

    monkeypatch.setattr(units.bot, "get_chat_member", get_chat_member)
    monkeypatch.setattr(units, "bot_id", AsyncMock(return_value=42))
    monkeypatch.setattr(units, "_TITLE_FETCH_DELAY", 0)


def _kept(groups: list[dict], **kw) -> list[int]:
    revives = title_reactivations(groups, **kw)
    return [r["group"]["group_id"]
            for r in asyncio.run(units._still_in_the_chat(revives))]


def test_a_chat_the_bot_left_or_was_kicked_from_is_not_revived(monkeypatch):
    groups = [_off(1, "2444", "2444 ROSA, YADRIEL / GUERRERO, IVAN (ML)"),
              _off(2, "8134", "8134 NOVOA JIMENEZ, FRANKLIN / OCAMPO, HECTOR"),
              _off(3, "1225", "1225 / MAGAN")]
    _members(monkeypatch, {1: "left", 2: "kicked", 3: "member"})

    assert _kept(groups, unattended=True) == [3]


def test_membership_that_cannot_be_read_keeps_the_group_off(monkeypatch):
    """Off is where it already is, and "couldn't ask" is never an answer."""
    _members(monkeypatch, {1: RuntimeError("Forbidden: bot was kicked")})

    assert _kept([_off(1, "1225", "1225 / MAGAN")]) == []


def test_a_muted_bot_is_still_in_the_chat(monkeypatch):
    """Muted is group_health's problem, not a reason to keep a truck off."""
    _members(monkeypatch, {1: "restricted", 2: "administrator"})

    assert _kept([_off(1, "1225", "1225 / A"), _off(2, "1226", "1226 / B")]) == [1, 2]


def test_restricted_but_no_longer_a_member_is_gone(monkeypatch):
    """Telegram reports someone who left while restricted as "restricted"."""
    _members(monkeypatch, {1: SimpleNamespace(status="restricted", is_member=False)})

    assert _kept([_off(1, "1225", "1225 / A")]) == []


def test_nothing_to_revive_asks_telegram_nothing(monkeypatch):
    asked = AsyncMock()
    monkeypatch.setattr(units.bot, "get_chat_member", asked)

    assert asyncio.run(units._still_in_the_chat([])) == []
    asked.assert_not_awaited()


def _sweep(monkeypatch, groups: list[dict]):
    """Stub run_title_sweep's reads and writes; return (write, sent)."""
    titles = {g["group_id"]: g["title"] for g in groups}
    monkeypatch.setattr(units, "get_all_groups", AsyncMock(return_value=groups))
    monkeypatch.setattr(units.bot, "get_chat", AsyncMock(
        side_effect=lambda gid: SimpleNamespace(title=titles[gid])))
    monkeypatch.setattr(units, "set_group_title", AsyncMock())
    write = AsyncMock(return_value=(0, 0, 1))
    monkeypatch.setattr(units, "apply_title_sweep", write)
    sent = AsyncMock()
    monkeypatch.setattr(units.bot, "send_message", sent)
    monkeypatch.setattr(units, "_ADMIN_IDS", [7])
    return write, sent


def test_the_sweep_neither_writes_nor_reports_a_kicked_chat(monkeypatch):
    """The loop itself: the title reads fine, the bot is gone, nothing happens."""
    groups = [_off(-5252338165, "2444", "2444 ROSA, YADRIEL / GUERRERO, IVAN (ML)")]
    write, sent = _sweep(monkeypatch, groups)
    _members(monkeypatch, {-5252338165: "left"})

    assert asyncio.run(units.run_title_sweep()) is None
    write.assert_not_awaited()
    sent.assert_not_awaited()


def test_the_sweep_still_revives_a_chat_the_bot_is_in(monkeypatch):
    groups = [_off(-5597036016, "4037", "4037 DIIS, SIYAD M / KHALIF, ABDULLAHI M")]
    write, sent = _sweep(monkeypatch, groups)
    _members(monkeypatch, {-5597036016: "member"})

    report = asyncio.run(units.run_title_sweep())

    assert write.await_args.args == ([], [], [(-5597036016, "4037")])
    assert "1 group(s) reactivated" in report
    sent.assert_awaited_once()
