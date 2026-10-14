"""Which AI model runs is the super-admin's alone (2026-10-14).

A regular admin is not shown the model card at all, and the API refuses them
the read as well as the switch -- hiding it in the page alone would leave the
same answer one request away.
"""
import asyncio
import json
from pathlib import Path

from webapp import server


class _Req(dict):
    def __init__(self, is_super_admin, body=None):
        super().__init__(admin={"user_id": 1, "is_super_admin": is_super_admin})
        self._body = body

    async def json(self):
        return self._body


def _call(handler, **kwargs):
    resp = asyncio.run(handler(_Req(**kwargs)))
    return resp.status, json.loads(resp.body)


def test_a_regular_admin_cannot_read_the_model():
    status, _ = _call(server.api_model, is_super_admin=False)
    assert status == 403


def test_a_regular_admin_cannot_switch_it_either():
    status, _ = _call(server.api_model_set, is_super_admin=False,
                      body={"model": "anything"})
    assert status == 403


def test_the_super_admin_still_reads_it():
    status, body = _call(server.api_model, is_super_admin=True)
    assert status == 200 and body["active"] and body["available"]


def test_the_page_only_asks_for_the_model_as_a_super_admin():
    page = (Path(server.__file__).parent / "static" / "index.html").read_text(
        encoding="utf-8")
    tools = page[page.index("async function renderTools"):]
    tools = tools[:tools.index("\n}\n")]
    fetch = tools.index('api("/api/model")')
    assert "if (me.is_super_admin)" in tools[:fetch]
    assert "Only super-admins can switch" not in page


# ---------- every super-admin endpoint, not just the model ----------
#
# `_require_super` returns an aiohttp Response, which is *falsy* while it holds
# no per-response state -- so `if resp := _require_super(...)` never fired and
# all five of these answered a regular admin until 2026-10-14.

class _AdminReq(_Req):
    match_info = {"uid": "555"}


def test_a_falsy_response_is_why_the_gate_must_test_for_none():
    resp = server._require_super(_Req(is_super_admin=False))
    assert resp is not None and resp.status == 403
    assert not resp  # the trap itself


def test_a_regular_admin_cannot_list_add_or_remove_admins(monkeypatch):
    for name in ("get_admins", "add_admin", "remove_admin"):
        monkeypatch.setattr(server, name, _boom)
    for handler in (server.api_admins, server.api_admins_add,
                    server.api_admins_remove):
        resp = asyncio.run(handler(_AdminReq(False, body={"user_id": 555})))
        assert resp.status == 403, handler.__name__


async def _boom(*args, **kwargs):
    raise AssertionError("a regular admin reached a super-admin write")
