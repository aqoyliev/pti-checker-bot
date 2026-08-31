"""The chat menu button that opens the Mini App.

Pure: the Telegram call is a mock. What matters here is *who* gets the button —
the panel it opens refuses non-admins, so offering it to a driver would be a
shortcut to a rejection.
"""
import asyncio
from unittest.mock import AsyncMock

from aiogram.types import MenuButtonDefault, MenuButtonWebApp

from handlers.admin import panel

URL = "https://example.up.railway.app"


def _wire(monkeypatch, *, is_admin: bool, url: str = URL):
    monkeypatch.setattr(panel, "WEBAPP_URL", url)
    monkeypatch.setattr(panel, "_is_admin", AsyncMock(return_value=is_admin))
    set_button = AsyncMock()
    monkeypatch.setattr(panel.bot, "set_chat_menu_button", set_button)
    return set_button


def test_an_admin_gets_a_web_app_button_labelled_open(monkeypatch):
    set_button = _wire(monkeypatch, is_admin=True)
    asyncio.run(panel.sync_menu_button(7564871221))

    set_button.assert_awaited_once()
    kwargs = set_button.await_args.kwargs
    assert kwargs["chat_id"] == 7564871221
    button = kwargs["menu_button"]
    assert isinstance(button, MenuButtonWebApp)
    assert button.text == "Open"
    assert button.web_app.url == URL


def test_a_driver_is_reset_rather_than_offered_the_panel(monkeypatch):
    # Reset, not skipped: someone removed from the admin list must lose the
    # shortcut instead of keeping a button that only ever answers "no".
    set_button = _wire(monkeypatch, is_admin=False)
    asyncio.run(panel.sync_menu_button(999))

    assert isinstance(set_button.await_args.kwargs["menu_button"], MenuButtonDefault)


def test_no_webapp_url_means_no_call_at_all(monkeypatch):
    # Telegram requires HTTPS for Mini Apps, so an unset WEBAPP_URL has nothing
    # to point at — and the inline panel keeps working either way.
    set_button = _wire(monkeypatch, is_admin=True, url="")
    asyncio.run(panel.sync_menu_button(7564871221))

    set_button.assert_not_awaited()


def test_a_telegram_failure_is_swallowed(monkeypatch):
    # The greeting and the panel must not break because a menu button didn't set.
    set_button = _wire(monkeypatch, is_admin=True)
    set_button.side_effect = RuntimeError("Bad Request: chat not found")

    asyncio.run(panel.sync_menu_button(7564871221))  # must not raise
