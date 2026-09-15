"""The report PDF's one-time download token.

Telegram's mobile in-app browser can't save a file behind a synthetic
<a download> click, so the report buttons now open the PDF's URL with
tg.openLink() -- a plain navigation that can't carry the "tma" Authorization
header. This token stands in for it on that one GET: minted from an
already-authenticated call, and single-use so it can't be replayed.

Pure: no network, no database.
"""
import asyncio
import time

from webapp import server


def _call(handler, **kwargs):
    from tests.test_webapp_reports import _Req
    return asyncio.run(handler(_Req(**kwargs)))


def test_a_fresh_token_resolves_to_the_admin_that_minted_it():
    admin = {"user_id": 42, "is_super_admin": False}
    token = server._mint_download_token(admin)

    assert server._consume_download_token(token) == admin


def test_a_token_is_single_use():
    token = server._mint_download_token({"user_id": 1})
    server._consume_download_token(token)

    assert server._consume_download_token(token) is None


def test_an_unknown_token_resolves_to_nothing():
    assert server._consume_download_token("not-a-real-token") is None


def test_an_expired_token_resolves_to_nothing(monkeypatch):
    token = server._mint_download_token({"user_id": 1})
    expires, admin = server._download_tokens[token]
    server._download_tokens[token] = (time.monotonic() - 1, admin)

    assert server._consume_download_token(token) is None


def test_the_mint_endpoint_hands_back_a_usable_token():
    resp = _call(server.api_report_token)
    import json
    token = json.loads(resp.body)["token"]

    assert server._consume_download_token(token) == {"user_id": 1, "is_super_admin": True}
