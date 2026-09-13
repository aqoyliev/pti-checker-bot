"""``/admin`` -- the door to the web panel.

The panel itself is the Telegram Mini App in ``webapp/``. This used to carry a
full inline-keyboard copy of it (groups, stats, broadcast, model, admins);
that copy was removed on 2026-09-13 once every deployment had ``WEBAPP_URL``
set -- two panels with the same features drift, and the inline one had
already fallen behind. What is left is the way in: the ``/admin`` command
with a web_app button, and the chat menu button that points at the same URL.
"""
from __future__ import annotations

import logging

from aiogram import types
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonDefault,
    MenuButtonWebApp,
    WebAppInfo,
)

from data.config import WEBAPP_URL
from loader import bot, dp
from utils.admins import is_admin


async def sync_menu_button(user_id: int) -> None:
    """Point an admin's chat menu button at the Mini App, so the panel is one
    tap away instead of a remembered /admin.

    Per chat, never the bot-wide default: almost everyone who opens a DM with
    this bot is a driver, and a global web_app button would offer every one of
    them a panel that then refuses them. A non-admin is *reset* rather than
    skipped, so someone removed from the admin list loses the shortcut on their
    next /start instead of keeping a button that only ever answers "no".

    Best effort. The button in /admin is the real entrypoint and works whether
    or not Telegram accepts this.
    """
    if not WEBAPP_URL:
        return
    button = (MenuButtonWebApp(text="Open", web_app=WebAppInfo(url=WEBAPP_URL))
              if await is_admin(user_id) else MenuButtonDefault())
    try:
        await bot.set_chat_menu_button(chat_id=user_id, menu_button=button)
    except Exception:
        logging.exception("could not set the menu button for %s", user_id)


@dp.message_handler(commands=["admin"], chat_type=types.ChatType.PRIVATE)
async def cmd_admin(message: types.Message):
    if not await is_admin(message.from_user.id):
        return  # stay silent so the panel isn't discoverable by non-admins
    if not WEBAPP_URL:
        # Telegram only opens Mini Apps over HTTPS, so without a public URL
        # there is nothing to point a button at.
        await message.answer(
            "The web panel isn't configured on this deployment — set "
            "<code>WEBAPP_URL</code> to the bot's public HTTPS address.",
            parse_mode="HTML")
        return
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("🌐 Open the admin panel", web_app=WebAppInfo(url=WEBAPP_URL)))
    await message.answer(
        "<b>🛠 PTI admin panel</b>\n\nGroups, drivers, compliance, reports, "
        "broadcasts and the AI model are all in the web panel.",
        reply_markup=kb)
    await sync_menu_button(message.from_user.id)
