import logging

from aiogram import types

from data.config import ADMINS

_DEFAULT_COMMANDS = [
    types.BotCommand("start", "Start the bot"),
    types.BotCommand("help", "Help"),
    types.BotCommand("check", "Run PTI inspection on a replied video or photo"),
    types.BotCommand("adddriver", "Register a driver in this group"),
    types.BotCommand("setunit", "Set the current truck unit number"),
]


async def set_default_commands(dp):
    await dp.bot.set_my_commands(_DEFAULT_COMMANDS)

    # Add /admin only in each admin's private chat, so drivers never see it.
    admin_commands = _DEFAULT_COMMANDS + [types.BotCommand("admin", "Open the admin panel")]
    for admin in ADMINS:
        try:
            await dp.bot.set_my_commands(
                admin_commands,
                scope=types.BotCommandScopeChat(chat_id=int(admin)),
            )
        except Exception:
            logging.warning("Could not set admin commands for %s (have they started the bot?)", admin)
