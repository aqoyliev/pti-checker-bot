import logging

from aiogram import types

from data.config import ADMINS

# /adddriver and /setunit only have handlers registered for group chats (see
# handlers/groups/registration.py) -- offering them in a private chat's command
# menu points at a command that silently does nothing there. Split the menu the
# same way handlers/users/help.py already splits its text.
_PRIVATE_COMMANDS = [
    types.BotCommand("start", "Start the bot"),
    types.BotCommand("help", "Help"),
    types.BotCommand("check", "Run PTI inspection on a replied video or photo"),
]

_GROUP_COMMANDS = [
    types.BotCommand("help", "Help"),
    types.BotCommand("check", "Run PTI inspection on a replied video or photo"),
    types.BotCommand("adddriver", "Register a driver in this group"),
    types.BotCommand("setunit", "Set the current truck unit number"),
]

# DM-only, admin-only commands from handlers/admin/*.py.
_ADMIN_EXTRA = [
    types.BotCommand("admin", "Open the admin panel"),
    types.BotCommand("whois", "Look up a phone number's Telegram account"),
    types.BotCommand("fixnames", "Store drivers under their fleet names"),
    types.BotCommand("units", "Update this week's active unit list"),
    types.BotCommand("titlecheck", "Check group titles for lost unit numbers"),
    types.BotCommand("retitle", "Re-file groups whose title now names another unit"),
    types.BotCommand("quiet", "List groups with little recent traffic"),
    types.BotCommand("onboard", "Re-open setup for a group"),
    types.BotCommand("nondrivers", "Manage the fleet-wide non-driver list"),
]


async def set_default_commands(dp):
    # Default scope is the fallback for any chat not covered by a more specific
    # scope below; keep it the same as the private-chat menu.
    await dp.bot.set_my_commands(_PRIVATE_COMMANDS)
    await dp.bot.set_my_commands(_PRIVATE_COMMANDS, scope=types.BotCommandScopeAllPrivateChats())
    await dp.bot.set_my_commands(_GROUP_COMMANDS, scope=types.BotCommandScopeAllGroupChats())

    # A per-chat scope outranks AllPrivateChats, so each admin's own DM gets the
    # admin extras layered on top of the plain private-chat menu.
    admin_commands = _PRIVATE_COMMANDS + _ADMIN_EXTRA
    for admin in ADMINS:
        try:
            await dp.bot.set_my_commands(
                admin_commands,
                scope=types.BotCommandScopeChat(chat_id=int(admin)),
            )
        except Exception:
            logging.warning("Could not set admin commands for %s (have they started the bot?)", admin)
