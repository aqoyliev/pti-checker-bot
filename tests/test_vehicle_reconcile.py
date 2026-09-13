"""What a PTI is allowed to write about the vehicles it filmed.

The trailer and the truck are treated differently on purpose, and the asymmetry
is easy to undo by accident:

* the **trailer** has no other source in the whole system — not the group's
  title, not a sweep, and it changes weekly — so the video's reading is stored;
* the **truck** comes from the group's title and the daily title sweep re-files
  it from there, so a PTI that overwrote it would be undone within a day and
  spend the time in between filing inspections under a unit nothing else agrees
  with. It is reported to the admins instead.

No network, no database: every write and send is captured.
"""
import asyncio
import types

import pytest

from handlers.groups import pti


class _Chat:
    def __init__(self, cid=-100, title="UNIT 2570"):
        self.id, self.title = cid, title


@pytest.fixture
def caught(monkeypatch):
    """Capture everything _reconcile_vehicles could write or send."""
    out = {"trailer": [], "plate": [], "admin": []}

    async def set_trailer(gid, unit, plate):
        out["trailer"].append((gid, unit, plate))

    async def set_truck_plate(gid, plate):
        out["plate"].append((gid, plate))

    async def notify(text, **kw):
        out["admin"].append(text)
        return 1

    monkeypatch.setattr(pti, "set_trailer", set_trailer)
    monkeypatch.setattr(pti, "set_truck_plate", set_truck_plate)
    monkeypatch.setattr(pti, "notify_super_admins", notify)
    return out


def _run(group: dict, vehicles: list[dict], monkeypatch):
    async def get_group(gid):
        return group

    monkeypatch.setattr(pti, "get_group", get_group)
    message = types.SimpleNamespace(chat=_Chat())
    asyncio.run(pti._reconcile_vehicles(message, {"vehicles": vehicles}))


GROUP = {"group_id": -100, "unit_number": "2570", "truck_plate": "ABC123",
         "trailer_unit": None, "trailer_plate": None}


# ---------- the trailer is stored ----------

def test_a_filmed_trailer_is_written(caught, monkeypatch):
    _run(GROUP, [{"type": "trailer", "unit_number": "53821", "plate": "T-99"}],
         monkeypatch)
    assert caught["trailer"] == [(-100, "53821", "T-99")]


def test_a_new_trailer_replaces_the_stored_one(caught, monkeypatch):
    _run({**GROUP, "trailer_unit": "53821"},
         [{"type": "trailer", "unit_number": "77120", "plate": None}], monkeypatch)
    assert caught["trailer"] == [(-100, "77120", None)]


def test_the_same_trailer_is_not_rewritten_every_inspection(caught, monkeypatch):
    # Both sides are normalized before comparing, so a reading that comes back
    # with the angle brackets or spaces the fleet's imports left behind is not
    # a change.
    _run({**GROUP, "trailer_unit": "53821"},
         [{"type": "trailer", "unit_number": " <53821> ", "plate": None}], monkeypatch)
    assert caught["trailer"] == []


def test_a_trailer_plate_alone_still_lands(caught, monkeypatch):
    _run({**GROUP, "trailer_unit": "53821"},
         [{"type": "trailer", "unit_number": None, "plate": "T-77"}], monkeypatch)
    assert caught["trailer"] == [(-100, None, "T-77")]


# ---------- the truck is reported, never rewritten ----------

def test_a_different_truck_is_reported_not_stored(caught, monkeypatch):
    _run(GROUP, [{"type": "truck", "unit_number": "1882", "plate": "XYZ789"}],
         monkeypatch)
    assert caught["plate"] == []
    assert len(caught["admin"]) == 1
    assert "2570" in caught["admin"][0] and "1882" in caught["admin"][0]


def test_the_truck_change_notice_does_not_reach_the_group(caught, monkeypatch):
    # message.answer is not even available on the fake: a driver's inspection
    # did not change, so nothing about this belongs in their chat.
    _run(GROUP, [{"type": "truck", "unit_number": "1882", "plate": "XYZ789"}],
         monkeypatch)
    assert "Send Messages" not in "".join(caught["admin"])


def test_a_misread_unit_is_silent(caught, monkeypatch):
    # Same plate, different unit — one truck filmed badly. Nobody is told.
    _run(GROUP, [{"type": "truck", "unit_number": "1882", "plate": "ABC123"}],
         monkeypatch)
    assert caught["admin"] == [] and caught["plate"] == []


def test_a_new_plate_on_the_same_truck_is_stored_quietly(caught, monkeypatch):
    _run(GROUP, [{"type": "truck", "unit_number": "2570", "plate": "XYZ789"}],
         monkeypatch)
    assert caught["plate"] == [(-100, "XYZ789")]
    assert caught["admin"] == []


def test_nothing_read_writes_nothing(caught, monkeypatch):
    _run(GROUP, [], monkeypatch)
    assert caught == {"trailer": [], "plate": [], "admin": []}


def test_an_unknown_group_writes_nothing(caught, monkeypatch):
    _run(None, [{"type": "trailer", "unit_number": "53821", "plate": None}],
         monkeypatch)
    assert caught["trailer"] == []


def test_nothing_here_can_overwrite_the_registered_unit():
    source = open(pti.__file__, encoding="utf-8").read()
    assert "set_truck_unit" not in source
    assert "set_group_unit" not in source
